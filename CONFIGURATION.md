# Configuration

`llm-handoff` is expected to use a YAML config file named
`dispatch_config.yaml` in the target repository.

This document describes the public configuration surface currently parsed by
the source checkout. Runtime role-to-provider dispatch is config-driven for the
registered CLI adapters. See [dispatch_config.example.yaml](dispatch_config.example.yaml)
for a copyable example shape.

## Minimal Shape

```yaml
handoff_path: docs/handoff/HANDOFF.md
project_state_path: PROJECT_STATE.md
backend_resume: true
planner_resume: true

agents:
  planner:
    provider: antigravity
    binary: agy
    resume: true
    timeout_ms: 1200000

  backend:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: true
    timeout_ms: 1200000

  auditor:
    provider: claude
    binary: claude
    model: claude-opus-5-5
    resume: false
    timeout_ms: 900000
```

## Handoff Path

Default:

```yaml
handoff_path: docs/handoff/HANDOFF.md
```

Flat alternative:

```yaml
handoff_path: HANDOFF.md
```

The nested path is the recommended default because it leaves room for handoff
examples, archive files, and prompt handoff helpers. The flat path is useful for
small repos that prefer fewer directories.

Both `handoff_path` and `project_state_path` must be relative paths that stay
inside the repository. Config load rejects an empty value, a value with
control characters, an absolute path (`/x` or `\x`), a UNC path
(`\\server\share` or `//server/share`), a drive-letter path (`C:\x`, `C:/x`,
or `C:x`), a value with a `..` segment, a value that names the repository root
(such as `.`), and a value that resolves outside the repository through a
symlink. Forward slashes, backslashes, and a leading `./` are accepted.

## Project State Path

Recommended default:

```yaml
project_state_path: PROJECT_STATE.md
```

This file is an optional durable project-status document. Consumers can point it
at an existing file if their workflow already uses one. The finalizer prompt
names this configured path when it tells the finalizer which file to update
and commit, and tells it to add every story that `remaining_stories` names and
this file does not list yet.

After each dispatch, the post-dispatch gate warns
(`remaining_story_not_in_project_state`) when `remaining_stories` names a
story ID that this file does not contain, or a title-only story whose full
title it does not contain. A story that exists only in the
handoff is lost when a later rewrite drops it, and in dispatcher commit mode
the planner's own edits to this file are never committed, so the planner
records new stories through the next Work Packet. The warning does not fail
the gate. It is skipped when the file does not exist and in Linear mode, where
the tracker holds the stories.

## Auto Push

The `auto_push` key has been removed. The dispatcher never read it. An
existing `auto_push: false` line is still accepted and ignored, so older
configs keep loading. `auto_push: true`, or any value other than `false`, is
rejected when the config loads; delete the key.

The dispatcher pushes only through `publish.mode`. With the default
`publish.mode: local`, it never pushes, and it publishes to a remote only when
a user sets `publish.mode` to opt in.

Agents are told not to push, and the reference workflow denies `git push` through
provider guardrails, but neither is a hard boundary. Role allowlists are a
guardrail, not a sandbox: any interpreter grant (see
[Interpreter Grants](#interpreter-grants)) can run any command, `git push`
included. Protect the remote with server-side branch protection, meaning a
ruleset that requires pull requests and has no bypass actors. The dispatcher
does not configure it. See also [SECURITY.md](SECURITY.md#threat-model).

## Runtime Controls

The dispatcher exposes role-based runtime toggles for session reuse:

```yaml
backend_resume: true
planner_resume: true
```

Provider-named runtime keys from the source extraction are not supported in the
public config. Use the role-based keys shown above.

The `planner_api_key_env` key was removed with the gemini provider, the only
adapter that read it. An existing `planner_api_key_env: false` line is still
accepted and ignored, so older configs keep loading. Any other value fails at
load with: "planner_api_key_env was removed with the gemini provider: Gemini
CLI no longer serves personal Google accounts. Delete the planner_api_key_env
key; a Codex role uses use_api_key_env: true for OPENAI_API_KEY." The
`--use-planner-api-key-env` command-line flag was removed too; passing it
exits with code 2 before any config loads.

## Pause Recovery

`pause_recovery` controls what the dispatcher does when it pauses:

```yaml
pause_recovery: planner   # planner (default) | "off"
```

With `planner`, the dispatcher runs bounded planner recovery at these six
pauses in the run loop:

- unknown route: no routing instruction in `HANDOFF.md`;
- stale route: the same route on an unchanged `HANDOFF.md`;
- low confidence: a routing decision below `HIGH` confidence;
- unparseable finalizer output from the ledger updater;
- missing route after dispatch: the planner left no dispatchable route;
- self-loop: a role routed the handoff back to itself.

At each of these pauses the validator still runs first. The dispatcher then
logs a `PAUSE` line and dispatches the planner on the next cycle instead of
waiting. The planner's instruction carries the pause reason and the
validator's verdict, errors, and warnings. The planner either rewrites
`HANDOFF.md` with a dispatchable route or routes to `user` with
`status: escalate_to_user` and one specific question. The recovery cycle skips
the stale-route check, so a stale planner route cannot pause on its own
recovery.

Each pause streak gets one recovery attempt. If the handoff still pauses
after it, the dispatcher pauses as it does with `off` and waits for
`HANDOFF.md` to change. The streak resets only after a normal dispatch passes:
one dispatched without a dispatcher-forced instruction whose post-dispatch
validation verdict is not `NO`, or a finalizer run with parseable output and
exit code 0. A recovery cycle, or any other cycle with a forced planner
instruction, never resets it.

A handoff routed to `user` (`next_agent: user`) never triggers recovery; the
dispatcher always waits for a person. The same holds when the pause validator
leaves `HANDOFF.md` routed to `user`, for example after it changed a protected
file and the dispatcher wrote the protected-file escalation. `--dry-run` never
schedules recovery. In Linear mode, a pause diagnosis whose `LINEAR-EVIDENCE`
row fails never triggers recovery, and the dispatcher waits for a person. A
validator report with no `LINEAR-EVIDENCE` row, or with a row that does not
parse (such as `LINEAR-EVIDENCE: FAIL` with no detail), counts as a failed
row: recovery needs a parsed `PASS` or `WARN` status.

Set `pause_recovery: "off"` to skip recovery and pause at once, as earlier
releases did. Quote the value: YAML reads a bare `off` as the boolean `false`,
which the config rejects. The startup banner shows the active mode.

## Operator Prompt

`interactive_pause` controls the operator prompt at dispatcher pauses:

```yaml
interactive_pause: true   # true (default) | false
```

When the dispatcher pauses and waits for `HANDOFF.md` to change, it logs the
`PAUSE` reason line and then, if a person is at a terminal, shows an operator
prompt. The prompt appears at every `next_agent: user` pause, and at the other
pauses once bounded planner recovery is used up or unavailable (for example
with `pause_recovery: "off"`). It prints one line:

```text
Press Enter to have the planner fix this, or type instructions for it and press Enter.
```

When the planner has already tried in the current pause streak, through
bounded recovery or an earlier operator dispatch, a second line follows:

```text
Or edit HANDOFF.md yourself; the loop resumes when it changes.
```

Above the prompt, the reason line drops a generic "Update the handoff;
dispatch will resume after the file changes." tail, since the prompt covers
it. Reasons that end with specific steps, such as a story branch or a publish
merge to finish, keep them. The planner and role instructions always get the
full reason.

The prompt accepts five actions. Only the first two are printed; the rest
are listed here:

- Enter: dispatch the planner with an instruction to resolve the pause with
  its best judgment.
- Any other text: dispatch the planner with that text as guidance.
- `@backend`, `@frontend`, or `@auditor`, optionally followed by a message:
  dispatch that role directly on the current `HANDOFF.md`, bypassing the
  handoff's route. See "Role targeting" below.
- `retry`: re-run the cycle. On a `user` route this re-reads `HANDOFF.md`,
  which still routes to `user`, so the dispatcher pauses again.
- `quit`: stop the dispatcher with exit code 0.

Editing `HANDOFF.md` still resumes the loop, as it does without the prompt.
The planner instruction states the pause reason and that a person at the
dispatcher console sent it. Like bounded planner recovery, it forbids changes
to source, tests, project-state files, ledger files, and completed work: the
planner rewrites `HANDOFF.md` with a dispatchable route, or routes to `user`
with `status: escalate_to_user` and one specific question. The operator
planner cycle skips the stale-route check and does not reset or use up the
pause recovery streak.

In the planner and role instructions, the pause reason is passed between
`<<<PAUSE REASON>>>` and `<<<END PAUSE REASON>>>` markers and the operator's
message between `<<<OPERATOR MESSAGE>>>` and `<<<END OPERATOR MESSAGE>>>`
markers, with a sentence saying the first is quoted dispatcher state and the
second cannot widen the role's boundaries. Any run of three or more `<` or `>`
characters inside the quoted text is split with spaces, so the text cannot
close its block or open a new one.

The dispatcher reads the terminal only while a prompt is shown, so nothing
competes with the keypress a `--manual-frontend` step waits for. Lines typed
while no prompt is shown, for example while an agent runs or before the first
prompt, are discarded when the next prompt appears; the dispatcher logs how
many.

Role targeting: a line that starts with `@` names a role, matched
case-insensitively, up to the first space; the rest of the line is the
message. `@backend`, `@frontend`, and `@auditor` schedule one dispatch of
that role on the current `HANDOFF.md`. The role's instruction states the
pause reason, that a person at the dispatcher console dispatched it from the
operator prompt, and the operator's message (`Operator instruction: ...`), or,
with no message, a request to continue the assignment in the handoff. It also
tells the role to stay within its boundaries and the Work Packet's Files in
scope and to write its normal handback. When `audit.reviewers` includes
`security`, `@auditor` runs the read-only audit fan-out instead, so its
instruction keeps the operator's message but tells the reviewer not to edit
`HANDOFF.md` and to return only the structured JSON the review requests.
`@planner <message>` is the same as
typing the message as planner guidance, and `@planner` alone is the same as
pressing Enter. Any other target, such as `@user`, `@finalizer`, or
`@validator`, is rejected with a message that lists the accepted targets, and
the prompt keeps waiting.

A targeted dispatch skips the stale-route check, so it runs even when the
same route already ran on an unchanged handoff. Everything else applies as
for a normal dispatch of that route: the three-fix circuit breaker for
`backend` and `frontend`, the story branch, the work-claim gate, dispatcher
commits, the protected-file check, and post-dispatch validation. Like the
operator planner cycle, it does not reset the pause recovery streak. If
`HANDOFF.md` changes before the targeted dispatch runs, the dispatcher drops
it, logs a `WARN` line, and routes the current handoff normally.

The prompt never appears when stdin is not a TTY, in `--dry-run`, or with
`poll_interval_seconds: 0`. In those cases the pause behaves as before. When
stdin is a TTY but the dispatcher has no console input for it, the
dispatcher logs a `WARN` line and also waits for `HANDOFF.md` to change. To
turn the prompt off, set `interactive_pause: false` or pass
`--no-interactive-pause` on the command line. The startup banner shows
whether the prompt is enabled.

## Stopping The Dispatcher

To stop a running dispatcher without timing it, run this from a second
terminal in the target repository (or pass `--repo-root`):

```bash
llm-handoff stop
```

The command writes `logs/dispatch/stop-request`. The dispatcher checks for
that file before each cycle and once per poll while it waits at a pause or
at the operator prompt. It never interrupts an agent: a cycle in flight
finishes first, including, for an implementer, the dispatcher commit of its
in-scope files (dispatcher commit mode), post-dispatch validation, the
work-claim gate, and any publish step after an approval. The dispatcher then
removes the file, releases
`.dispatch.lock`, and exits with code 0 before it starts the next dispatch.
It logs one `PAUSE` line when it first sees the request and one when it
exits for it. Deleting the file before then cancels the request. The request
names the pid recorded in `.dispatch.lock`, and a dispatcher honors only a
request for its own pid (or one that names no pid, such as a file created by
hand). A request the run never honored is removed when that run exits. At
startup the dispatcher removes, with a `WARN` line, any request that does not
name its pid, so a request accepted for the new lock is kept. A request that
names no pid is honored by design: only one dispatcher can hold
`.dispatch.lock` in a repository, so a hand-made `logs/dispatch/stop-request`
stops the dispatcher running there, and one written before that dispatcher
started is removed at startup.

`llm-handoff stop` refuses, and writes nothing, when no `.dispatch.lock`
exists, when the lock records no pid (after giving a starting dispatcher a
moment to write it), when the lock's pid is not running, or when Git does not ignore
`logs/dispatch/stop-request`. An untracked file that Git does not ignore would
show up as an uncommitted file in the work-claim gate of the dispatch in
flight. `llm-handoff init` adds `/logs/dispatch/` to `.gitignore`.

Under [supervised dispatch](docs/SUPERVISED_DISPATCH.md) the request stops the
child dispatcher. The wrapper treats that exit like any other exit code 0: if
the run's output contains a failure marker such as a failed post-dispatch
gate, it may still start a repair and restart the child.

Agent processes start in their own process group (`CREATE_NEW_PROCESS_GROUP`
on Windows, a new session on POSIX), so a Ctrl+C in the dispatcher's console
reaches only the dispatcher. While a role's cycle runs, from its dispatch
through the dispatcher commit, the gates, and any publish step, the first
Ctrl+C asks for the same graceful stop as `llm-handoff stop`. The dispatcher
prints `Stop requested; finishing the current <role> run. Press Ctrl+C again
to stop now.`, finishes the cycle, and exits with code 0 before the next
dispatch. It logs the same `PAUSE` lines as for a stop request and writes no
file. A second Ctrl+C stops at once: the dispatcher kills the running agent's
process tree, releases the lock, and exits with code 130. Edits an
implementer stopped this way left are not recorded for carry-over, but the
next start can recover them from the in-flight record after you confirm it
(see [Dispatcher Commits](#dispatcher-commits)); review them with
`git status` before restarting.

Three limits apply:

- Ctrl+C outside a role's cycle still stops at once, with exit code 130:
  at startup, between cycles, while the dispatcher waits at a pause, and at
  the operator prompt.
- Under [supervised dispatch](docs/SUPERVISED_DISPATCH.md), Ctrl+C reaches
  only the supervisor, which kills the child dispatcher's process tree. Use
  `llm-handoff stop` there for a graceful stop.
- `git` commands the dispatcher runs itself are not in a separate process
  group and still receive Ctrl+C.

If the dispatcher was killed, `.dispatch.lock` stays behind and the next start
refuses. The refusal names the pid in the lock, says whether that pid is
running on this machine, and gives the command to check it
(`Get-Process -Id <pid>` on Windows, `ps -p <pid>` elsewhere). The dispatcher
does not remove the lock itself: a pid can be reused, and a dispatcher started
from another environment (for example WSL against the same checkout) has pids
this one cannot see. Remove the lock only after confirming no dispatcher is
active.

What survives a restart, all under `logs/dispatch/`:

- per-story bounce counts (`bounce-counts.json`);
- the publish story state (`publish/story.json`), so a paused publish
  resumes, an interrupted `pr` publish can finish, and a publish of the same
  head reuses its review request time (`publish_requested_head`,
  `publish_requested_at`). See
  [Resuming An Interrupted Publish](#resuming-an-interrupted-publish);
- the carry-over record (`carry-over.json`), which applies only after the
  operator confirms it at a terminal; otherwise it is discarded with a `WARN`
  line and its files stay operator edits. See
  [Dispatcher Commits](#dispatcher-commits);
- the in-flight record (`in-flight.json`), which exists only while an
  implementer, planner, finalizer, or audit fan-out dispatch runs. A record found at startup means that
  role was interrupted mid-dispatch; it applies only after the operator
  confirms it at a terminal. See [Dispatcher Commits](#dispatcher-commits).

What does not survive: a scheduled planner recovery or operator-prompt
dispatch that had not run yet (the dispatcher logs a `WARN` line naming it),
the pause recovery streak, the managed planner session, and the in-memory
audit range base (outside `local` mode the audit range starts from the saved
story base instead; see [Audit Fan-Out](#audit-fan-out)). After a restart
the dispatcher routes `HANDOFF.md` again; a pause that still applies is detected again and gets its one bounded planner recovery.

## Work-Claim Gate

On by default:

```yaml
enforce_work_packet: true
work_packet_verification_timeout_seconds: 600
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enforce_work_packet` | `true` | Runs the work-claim gate. Setting it to `false` needs `work_packet_opt_out_reason`. |
| `work_packet_opt_out_reason` | unset | Required, non-empty text when `enforce_work_packet` is `false`; rejected while the gate is on. |
| `work_packet_verification_timeout_seconds` | `600` | Timeout in seconds for the Verification command. |
| `work_claim_env_passthrough` | `[]` | Extra environment variable names the Verification command and scanners receive. They always get only `PATH`, `HOME`, `USERPROFILE`, `SystemRoot`, `SystemDrive`, `TEMP`, `TMP`, `LANG`, `LC_ALL`, `TERM`, `USERNAME`, `USER`, `LOGNAME`, `COMSPEC`, `PATHEXT`, `WINDIR`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, and `SHELL`, plus these names. Names match case-insensitively. Vendor API keys (including `GEMINI_API_KEY` and `GOOGLE_API_KEY`) are rejected at config load and never passed; `CLAUDE_*` and `CODEX_*` names are never passed, even when listed. Other `GEMINI_*` names are not blocked: one reaches the command only when listed here. |
| `verification_allowed_prefixes` | `[]` | Command prefixes a Work Packet Verification command must start with, matched literally with single spaces between words. A command that matches none, or that contains a shell control character, fails `verification_not_allowed` and routes to `planner`. Empty runs any command, with a startup and `doctor` `WARN`. See [Verification Command Allowlist](#verification-command-allowlist). |

To turn the gate off, set `enforce_work_packet: false` and say why:

```yaml
enforce_work_packet: false
work_packet_opt_out_reason: "Planner prompts do not write Work Packets yet."
```

The reason is stripped of surrounding whitespace. A config that sets
`enforce_work_packet: false` without a reason, or with a whitespace-only
reason, fails to load with a validation error that names both
`enforce_work_packet` and `work_packet_opt_out_reason`. A config that sets a
reason while the gate is on also fails to load, because the reason only applies
when the gate is off. `commit_mode: dispatcher` still needs the gate on: with
`commit_mode: dispatcher` and `enforce_work_packet: false`, the config fails
with the `commit_mode: dispatcher requires enforce_work_packet: true` error even
when a reason is set. When the gate is off, every dispatcher start logs a
`WARN` line with `enforce_work_packet: false` and the reason, and
`llm-handoff doctor` prints `WARN work-claim gate:` with it. When it is on,
the doctor prints `PASS work-claim gate:`. The doctor validates the current
handoff with the same Work Packet requirement as the dispatch loop, so a
planner assignment the dispatcher would reject for an unparseable Work Packet
also fails `llm-handoff doctor` while the gate is on.

When `enforce_work_packet` is `true`, the dispatcher verifies every
`status: ready_for_review` handoff from `backend` or `frontend` before it
dispatches the auditor. The checks are deterministic and run against the
`## Work Packet` the implementer received:

| Check | Error prefix | Passes when |
| --- | --- | --- |
| Work Packet | `work_packet_missing`, `work_packet_unparseable` | the assignment carries `Files in scope`, `Files out of bounds`, and `Verification command` |
| `scope_sha` | `scope_sha_not_head` | `scope_sha` resolves to the current HEAD; commits above it that touch only the handoff file are tolerated |
| Diff scope | `scope_violation` | every file changed since dispatch is inside `Files in scope` or is the handoff file, and none is under `Files out of bounds` |
| Working tree | `uncommitted_changes` | no new uncommitted changes exist outside the handoff file |
| Verification allowlist | `verification_not_allowed` | `verification_allowed_prefixes` is empty, or every `Verification command` starts with a listed prefix and contains no shell control character |
| Verification | `verification_failed` | the `Verification command` exits 0 within the timeout |
| Changes | `no_changes` | an implementer claim's commits since dispatch change at least one file other than the handoff file; applies in both commit modes |
| Evidence SHA | `evidence_sha_mismatch` | the `Commit SHA verified` value in `## Verification Evidence` names the same commit as `scope_sha` |

Path lists accept files, directories (`src/`), and globs, separated by commas,
semicolons, or whitespace. A value that uses backticks or an exception phrase is
read as prose: only backticked entries and bare words that look like paths
(containing `/`, `.`, or a glob character) count. In `Files out of bounds`,
entries after "other than", "except", "excluding", "apart from", or "but not"
are dropped up to the next comma or semicolon, so "every file in `src/` other
than `src/app.py`" puts `src/` out of bounds and leaves `src/app.py` alone.
When a file matches both lists, the narrowest entry decides: an exact file
beats a directory or glob, and a longer entry beats a shorter one. A tie goes
to `Files out of bounds`.

On failure the dispatcher does not dispatch the auditor. It rewrites
`HANDOFF.md` with `status: verified_fail`, `producer: validator`, a quoted
`reason` naming the failed checks, and a `## Dispatcher Verification` section
with the evidence, then routes back to the implementer. The bounce increments
the story's bounce count, so the three-fix circuit breaker still applies. A
missing or unusable Work Packet routes the bounce to `planner` instead, because
the packet is the planner's artifact. Every bounce carries the original
`## Work Packet` forward verbatim, and the gate reads the packet from the
assignment, so the implementer cannot change it. If the same Verification
command fails again as the only failed check, and no commit since the previous
bounce changed a file other than the handoff, the bounce goes to `planner` as
well. So does a Verification command that `verification_allowed_prefixes`
rejects (`verification_not_allowed`).

Every line of `Verification command` runs as its own command. A line that is
only a connective (`and`, `or`, `then`, `plus`, `also`) or ends in `:` is prose,
and the packet is `work_packet_unparseable`. A line that quotes commands in
backticks, such as `` `cmd1`, then `` or `` `cmd1` and `cmd2` ``, runs each
quoted command when the text around them is only punctuation and `and`,
`then`, `plus`, or `also`. Any other text around a quoted command, including
`or`, makes the line prose. Planners should write one command per nested list
item.

With the gate enabled, post-dispatch validation also rejects any handoff that
routes work to an implementer without a parseable Work Packet;
`work_packet_missing` and `work_packet_unparseable` become errors instead of
warnings, so the problem surfaces before an implementer session is spent.

The verification command is agent-written text. The dispatcher runs it through
the shell in `repo_root` with its own permissions. The implementer that wrote
the claim already ran in the same repository with provider permissions
disabled, so this adds no new exposure, but review the planner's Work Packet
the way you review any agent-written instruction. The command is bounded by
`work_packet_verification_timeout_seconds`; its process tree is killed on
timeout, and the command, exit code, and a bounded output tail are written to
the dispatch log.

The Verification command and every scanner run with an allowlisted
environment, not the dispatcher's own environment: the base and platform
variables listed for `work_claim_env_passthrough` above, plus the names that
key lists. Tokens such as `GITHUB_TOKEN` in the shell that started the
dispatcher do not reach them. A Verification command or scanner that needs
another variable, such as a proxy setting or a tool's cache directory, needs
that name listed:

```yaml
work_claim_env_passthrough: [HTTPS_PROXY, SSL_CERT_FILE]
```

Entries must be valid environment variable names (`^[A-Za-z_][A-Za-z0-9_]*$`)
after surrounding whitespace is stripped; an empty entry or a vendor API key
name such as `OPENAI_API_KEY` fails config load. This limits what the command
can read from the environment only. It still runs as your user account and can
use credentials stored outside the environment.

### Verification Command Allowlist

`verification_allowed_prefixes` limits which Work Packet Verification commands
the gate runs. It is optional but recommended:

```yaml
verification_allowed_prefixes: ["python -m pytest", "npm test"]
```

- Matching is literal. Surrounding spaces and tabs are stripped from the
  command, which must then equal a prefix or start with a prefix followed by
  one space, case-sensitively. `python -m pytest` allows `python -m pytest`
  and `python -m pytest tests -q`, and rejects `python -m pytestx` and
  `python -c "..."`.
- Words must be separated by single spaces. A command containing a tab,
  another whitespace character other than a space, or a run of two or more
  spaces (`python  -m pytest`, `python -m pytest  -q`,
  `python -m pytest<TAB>tests`) is rejected with a `whitespace` reason. The
  rule matches the `agy` grants: `llm-handoff init` prints each grant as
  `command(regex:^<prefix>( .*)?$)`, which matches the exact command line, so
  the gate refuses every command that grant would refuse.
- Once the list is set, a command containing any of `&`, `|`, `;`, `<`, `>`,
  `$`, backtick, `(`, `)`, `%`, `!`, a carriage return, or a newline is
  rejected even if its first words match, so an allowed prefix cannot be
  chained to a second command. `%` and `!` are included because on Windows
  the command runs through `cmd.exe`, which expands `%NAME%` (and `!NAME!`
  with delayed expansion) before parsing. Entries are stripped of surrounding
  whitespace; an empty entry, an entry containing one of these characters, or
  an entry without single spaces between words fails config load.
- The gate checks every Verification command in the packet before it runs
  any. If one is rejected, none runs, the claim fails
  `verification_not_allowed`, and the bounce routes to `planner` with
  `status: verified_fail`, because only the planner can change the Work
  Packet. Git state and scanner configuration failures still escalate to
  `user` first.
- With the list empty, any command runs, as before. Every dispatcher start
  then logs `WARN Verification allowlist: OFF`, and `llm-handoff doctor`
  prints `WARN verification allowlist:`. With the list set, the start logs
  the prefixes and the doctor prints `PASS verification allowlist:`. Neither
  line appears while the gate is off.
- `llm-handoff init --verification-command "<cmd>"` writes the confirmed
  command as the single entry. `init` rejects a command that contains one of
  the characters above or, once stripped, a tab, other non-space whitespace,
  or a run of two or more spaces.

Scanner commands are operator-authored config and are not prefix-checked. The
dispatcher logs the exact text of each scanner command and each Verification
command before it runs.

### Dispatcher Commits

```yaml
enforce_work_packet: true
commit_mode: dispatcher   # agent (default) | dispatcher
```

With `commit_mode: dispatcher`, implementers (`backend`, `frontend`) never
write to `.git`. The dispatcher adds an instruction to each implementer launch
telling it to leave changes uncommitted, leave `scope_sha` empty, and write
`pending-dispatcher-commit` as the evidence commit SHA. After a
`ready_for_review` handback, and before handoff validation, the dispatcher:

1. lists files changed since dispatch, skipping the handoff file and every file
   that was already dirty before dispatch;
2. stages only files inside the Work Packet's `Files in scope` and outside
   `Files out of bounds`;
3. commits exactly those paths with `git commit --no-verify --only`, under your
   Git identity, so other staged changes and repository hooks are not involved;
4. writes the new commit into the handoff as `scope_sha` and as the
   `Commit SHA verified` value.

Files it did not stage stay in the working tree and fail the gate's worktree
check, which sends the claim back to the implementer. As in agent commit mode,
the gate's `no_changes` check fails a claim whose commits since dispatch change
nothing but the handoff. This mode requires `enforce_work_packet: true`.

Files an implementer dispatch leaves uncommitted are the story's work, not
operator edits. The dispatcher remembers their content hashes, and the next
implementer dispatch for the same `story_id` (or, when the handoff has no
`story_id`, the same `## Work Packet` text, which a bounce carries forward
verbatim) treats any of them that are still unchanged as its own changes: the
commit step can stage them, and the worktree check fails the claim if they stay
uncommitted. A file edited in between or a different story puts a file back in
the "already dirty before dispatch" group, which is never committed.

The dispatcher saves this record to `logs/dispatch/carry-over.json` after each
implementer dispatch and loads it at startup. It drops files from the record
once the dispatcher commits them, and deletes the record when nothing is left
or when an implementer dispatch starts for a different story.

Agent roles can write under `logs/`, so a record read from disk at startup is
not trusted on its own. The dispatcher first drops files that are no longer
dirty with the recorded content (and deletes the record if none are left).
Then, only when `interactive_pause` is on and the dispatcher runs at a
terminal with console input, it logs the story and files and asks
`Carry these over as <story>'s work? [y/N]`. Answering `y` or `yes` applies
the record with the same rule as within one run: only files whose content is
unchanged, for the same story key. Any other answer, end of input, no
terminal, or `interactive_pause: false` discards the record with a `WARN`
line, deletes it, and its files stay operator edits that are never
committed. A dry run logs the record and leaves it on disk without asking or
applying it. Story keys and paths are shown with control characters
escaped. The in-memory record kept between cycles of one run still applies
without asking. When Git does not ignore that
path, the record stays in memory only and the dispatcher logs a `WARN` line,
because the untracked file would otherwise show up in the next work-claim
gate. When `logs/dispatch/carry-over.json` or a directory above it inside the
repository is a symlink or junction, the dispatcher does not read, write, or
delete the record, so a link cannot point it at a file outside the
repository; it logs a `WARN` line and the files stay operator edits. On
Windows the dispatcher holds `logs` and `logs/dispatch` open without delete
sharing while it writes or deletes the record, so neither can be swapped for a
junction mid-operation. It opens each directory relative to its parent's open
handle rather than by path, and writes, renames, and deletes the record
relative to the open `logs/dispatch` handle through file handles it has
checked are inside that directory, so an empty `logs` or `logs/dispatch`
turned into a mount point in place is refused too (POSIX uses directory
handles opened without following links). Where neither mechanism is
available, the record stays in memory only:
the dispatcher leaves any `carry-over.json` on disk untouched and logs a
`WARN` line.

An implementer interrupted mid-dispatch (a second Ctrl+C, a closed window, or
a crash) never reaches the step that records its leftovers, so the dispatcher
also writes `logs/dispatch/in-flight.json` right before each `backend` or
`frontend` dispatch the work-claim gate applies to. The record names the role,
the story, HEAD, the pre-dispatch `HANDOFF.md` text, the operator's dirty
files with their content hashes, and the SHA-256 digests of the protected
control files (`protected_paths`). Planner, finalizer, and audit fan-out
dispatches write their own records (described below); the single-agent
auditor (`audit.reviewers` without `security`), validator, ungated, and
dry-run dispatches write none. Once the dispatch returns,
whatever its exit code, the dispatcher rewrites the record as a `handback`
record that also holds the `HANDOFF.md` text the implementer left and HEAD at
that moment, and deletes it when the cycle is finished: at the top of the next
cycle, or when the run returns from that cycle. When the handback rewrite
fails, the dispatcher deletes the record instead. When that delete fails too,
the dispatcher logs an `ERROR` line and stops before the dispatcher commit and
gates, so the record still describes an unfinished dispatch and the next start
offers its work for recovery. When the finished cycle's handback record cannot
be deleted, the dispatcher logs an `ERROR` line and stops. An operator
`@backend` or `@frontend` dispatch that continued the saved story from a
handoff without a `story_id` also records the rebuilt assignment, and its
recovery continues that saved story the same way. The record uses the same
race-safe writer and link
checks as `carry-over.json`, and it is read the same way: `logs` and
`logs/dispatch` are opened (or, on Windows, pinned) without following links
and the record is opened relative to `logs/dispatch` without following a link,
so a link swapped in after the first check is refused rather than read. The
opened file must be a single-link regular file of at most 8 MiB; a pipe, a
directory, a hard link, a larger file, or JSON nested too deeply to parse
counts as an invalid record. When
Git does not ignore the path or neither
race-safe mechanism is available, the dispatcher writes nothing, leaves any
file there untouched, and logs one `WARN` line per run. At startup, before the
carry-over record, a record found on disk is compared with the working tree:
the files that are dirty now but were not before (minus the handoff) are the
agent's files, an operator file whose content changed or that is no longer
dirty is a changed operator file, and the handoff counts as changed when its
text differs from the pre-dispatch text. A record that does not parse, or with
nothing changed, is deleted. A read that fails for another reason, such as a
sharing violation or a permission error, logs a `WARN` line and keeps the
record for a later start. A dry run logs what it found, says
`Not applied in dry run.`, and leaves the record on disk. Otherwise the
confirmation rule above applies: only with `interactive_pause` on and a
terminal with console input does the dispatcher log the role, story, agent
files, changed operator files, and whether the handoff will be restored, then
ask `Recover <role>'s interrupted <story> work? [y/N]`. The record lives in a
file a role can rewrite, so the dispatcher cannot verify that its handoff text
is the real pre-dispatch assignment. Right before the question it therefore
logs the assignment that confirming would hand the re-dispatched role, with
its Files in scope, Files out of bounds, Verification commands, and a short
SHA-256, each escaped, and says that confirming accepts that assignment as the
next Work Packet. Decline when it is not the assignment you expect. Any answer other than
`y` or `yes`, end of input, no terminal, or `interactive_pause: false`
discards the record with a `WARN` line; nothing is restored or carried over.
On `y`, a half-written handoff is replaced by the record's handoff text, the
agent's files become that story's carry-over record (the carry-over prompt is
skipped for this start), the record is deleted, and the same role
is dispatched again with a note that its previous run was interrupted and
lists its files. When an operator file changed, nothing is dispatched: the
first cycle pauses with an `INTERRUPTED DISPATCH:` reason that names those
files, which stay operator edits the dispatcher never stages or commits. The
handoff is restored only inside the repository: before the prompt, a handoff
path that passes through a symlink or junction discards the record with a
`WARN` line and no prompt, and the restore itself walks the handoff's
directories without following links and replaces the file by a rename inside
its own directory, as the carry-over writer does. If a link appears in
between, nothing is written or carried over, the record is deleted, and a
`WARN` line says the files stay operator edits. The restored handoff is
written as UTF-8 with LF line endings.

A `handback` record found at startup means the implementer returned but its
cycle was interrupted before the dispatcher commit, validation, the
work-claim gate, or publish finished. The dispatcher rewrites the record
right after the dispatcher commit step and again right before the work-claim
gate, with the HEAD and `HANDOFF.md` text at that moment, so the record can
describe a cycle interrupted after the dispatcher commit, including one
interrupted while the gate ran its verification commands or scanners.
Resuming such a record finds nothing new to commit, keeps the stamped
`scope_sha`, and re-runs validation and the work-claim gate (with scanners)
on the stamped claim. When that rewrite fails, the record is deleted and the
cycle goes on; when the delete fails too, the dispatcher logs an `ERROR`
naming the record and stops before the gate. An interrupt between the
dispatcher's `git commit` and that rewrite still leaves the earlier record,
which the next start discards as below. When HEAD differs from the HEAD the
record holds, or `HANDOFF.md` differs from the handback text it holds, the
cycle got past the record's state or a gate rewrote the handoff (for example
the gate's `## Dispatcher Verification` block or a bounce): the dispatcher logs
one `WARN` line naming the record, role, and story, resumes nothing, restores
no handoff, and deletes the record (a dry run keeps it); any uncommitted files
stay operator edits. When `protected_paths` is set but the record holds no
protected-file digests, the dispatcher discards it the same way, because the
resumed cycle could not check protected files. Otherwise the agent's files and
the changed operator files are found as for a `dispatch` record. A dry run logs them, says
`Not applied in dry run.`, and keeps the record. The same confirmation rule
applies: the dispatcher logs the summary, the assignment summary described
above, and a short SHA-256 of the handback, then asks
`Resume <role>'s interrupted <story> handback at the dispatcher commit? [y/N]`.
Every path and story shown is escaped. Any answer other than `y` or `yes`, end
of input, no terminal, or `interactive_pause: false` discards the record with
a `WARN` line; the files stay operator edits and nothing is staged. On `y`
the agent's files become that story's carry-over record and the record is
deleted. When an operator file changed, the first cycle pauses with an
`INTERRUPTED HANDBACK:` reason that names those files, which the dispatcher
never stages or commits. Otherwise the first cycle resumes the interrupted
cycle without dispatching the role again: it treats the dispatch as exit code
0, uses the record's pre-dispatch handoff (or, when the record holds one, its
continued story's assignment) as the assignment for the dispatcher commit and
the work-claim gate, rebuilds the pre-dispatch HEAD, operator files, and
carry-over files from the record, checks protected files against the
protected-file baseline described below (not a snapshot taken after the
restart, which would already hold the implementer's edits), and then runs the usual commit,
validation, gate, and publish. That assignment is resolved once: the assignment summary
shown before the question is of the same text the commit and gate use. The
start does not repair or normalize the confirmed handback before that cycle,
because a rewrite would no longer match the confirmed text; the cycle's usual
post-dispatch repair and `next_agent` normalization run on it before
validation. That cycle writes no in-flight record. In agent commit mode
there is no dispatcher commit; the rewrite before the gate still records the
current HEAD and handoff, so a resumed cycle runs the gate once.

A planner dispatch writes a `planner` record to the same path right before it
starts. It holds the cycle, HEAD, the checked-out branch (`head_branch`,
`null` on a detached HEAD), the `HANDOFF.md` text the planner starts from, and
the protected-file digests; it never has a `handback` phase, operator
files, carried-over files, or a separate assignment, and its story is empty
when that handoff names no story. The dispatcher deletes it as soon as the
planner returns, whatever its exit code; when that delete fails it logs an
`ERROR` line and stops. The planner lease-rebuild cycle in Linear mode and dry
runs write none, and the same one-`WARN`-per-run rule applies when the record
cannot be kept. At startup a `planner` record goes through the same link,
read, and validity checks. Next, with `protected_paths` set, the same
protected-baseline checks described below: a record without protected-file
digests, or whose HEAD is not a commit that is HEAD or an ancestor of HEAD, is
discarded with a `WARN` line (a dry run keeps it), and Git is not touched.
In dispatcher commit mode, the checkout is then compared with the record's
HEAD and branch, as the check after a planner dispatch that returned would,
but without changing anything: the record's HEAD comes from a file a role can
write, so it proves nothing. When the branch is unchanged and HEAD is on the
fetched `<remote>/<base_branch>` (the checked-out branch when `base_branch` is
null; nothing is fetched), every commit since the record was published there,
for example by an operator who fast-forwarded `main` to `origin/main`: one
`INFO` line names how many published commits were skipped, nothing is undone,
and the checks below run as usual. Otherwise a branch switch, rewritten
history, or a commit with other files logs an `ERROR` line and stops the run
before any dispatch,
keeping the record and the checkout so the next start checks again. Commits
since the record that change only the handoff are undone only after the
operator confirms the exact reset: with `interactive_pause` and a terminal,
the dispatcher logs the commits, the record's HEAD, and that confirming moves
the branch back with `git reset --soft` and unstages the handoff, then asks
``Undo the interrupted planner's handoff commits with `git reset --soft <sha>`? [y/N]``.
On `y` the commits are undone (one `WARN` line; the handoff stays
uncommitted); a checkout that moved again before the answer logs an `ERROR`
line and stops with nothing undone. Any other answer, end of input, no
terminal, or `interactive_pause: false` discards the record with a `WARN`
line and leaves HEAD, the index, and the handoff as they are. A dry run only
reports a moved checkout and what a real start would do with it. A record HEAD that is not a commit is not
reconciled. Then:

- When `HANDOFF.md` still equals the record's text, the dispatcher logs one
  `INFO` line that there is nothing to recover and deletes the record.
- When it changed and passes the planner's post-dispatch validation, the
  planner finished writing it before the interrupt: the dispatcher keeps it,
  logs one `INFO` line, and deletes the record without asking.
- When it is missing, empty, or fails that validation, a dry run logs the
  summary and keeps the record. Otherwise the confirmation rule above applies:
  the dispatcher logs the record path, the escaped validation error codes, the
  note that the half-written handoff will be replaced, the assignment summary,
  and (with `protected_paths` set) the protected-file disclosure, then asks
  `Restore the handoff from before planner's interrupted dispatch and run planner again? [y/N]`.
  Any answer other than `y` or `yes`, end of input, no terminal, or
  `interactive_pause: false` discards the record with a `WARN` line and leaves
  the handoff as it is, so the usual invalid-handoff handling runs. On `y` the
  handoff is restored with the same link-safe writer (a link on its path
  discards the record before the prompt; a failed restore recovers nothing),
  the record is deleted (when that delete fails, the dispatcher logs an
  `ERROR` line and stops before dispatching; the next start finds the
  restored handoff unchanged and deletes the record), and the first cycle
  dispatches the planner (routing
  source `interrupted_planner_recovery`) with a note that its previous run was
  interrupted and the handoff was restored, checking protected files against
  the baseline described below. A forced instruction the interrupted cycle
  carried, such as an operator `@planner` message or a routing-recovery
  instruction, is not kept.

A finalizer (epic-close ledger-updater) dispatch writes a `finalizer` record
to the same path right before it starts. It has the same shape as a `planner`
record (cycle, HEAD, `head_branch`, the `HANDOFF.md` text the finalizer starts
from, and the protected-file digests; `dispatch` phase only, no operator
files, carried-over files, or separate assignment). The dispatcher deletes it
as soon as the finalizer returns, whatever its exit code or output, before the
dispatcher-commit-mode handoff check; when that delete fails it logs an
`ERROR` line naming the record and stops. Dry runs write none. At startup a
`finalizer` record goes through the same link, read, validity, and
protected-baseline checks as a `planner` record, then:

- a. Git is compared with the record's HEAD and branch, read-only, in any
  commit mode. A finalizer may commit only the project state file
  (`project_state_path`), plus the handoff in agent commit mode, where it
  commits both atomically. A branch switch, history that is not a descendant
  of the record's HEAD, a record HEAD that is not a commit, or a commit since
  that HEAD that changes any other file (in dispatcher commit mode, the
  handoff included) logs one `ERROR` line naming the commits and files and
  stops before any dispatch, keeping the record. The dispatcher never resets
  or rewrites Git for a finalizer record. Otherwise commits since the
  record's HEAD count as a ledger commit.
- b. When the project state file differs from HEAD in the working tree or
  index, including a rename or deletion of it (or Git cannot tell), the
  interrupted finalizer may have left a partial ledger edit. The index entry
  and the file on disk are compared with HEAD's blob directly, so an edit
  hidden from `git status` by `assume-unchanged`, `skip-worktree`, or an
  ignore rule still counts as a
  partial ledger edit: the dispatcher logs one `ERROR` line naming the file,
  keeps the record, and stops; commit or discard the edit and start again. A
  dry run logs an `INFO` line instead and keeps the record.
- c. When `HANDOFF.md` changed, passes the finalizer's post-dispatch
  validation, and no longer routes to `finalizer`, the finalizer finished:
  one `INFO` line, the handoff is kept, and the record is deleted.
- d. With no ledger commit and `HANDOFF.md` unchanged, nothing was done: one
  `INFO` line, the record is deleted, and the loop dispatches the finalizer
  again from the unchanged handoff.
- e. Every other case (the handoff missing, empty, invalid, or still routing to
  `finalizer`, with or without a ledger commit) restores the record's handoff
  only after the operator confirms. A dry run logs the summary and keeps the
  record. A handoff path behind a link discards the record before the prompt.
  The dispatcher logs the summary, the assignment summary, the ledger commits
  and their files (when there are any), and the protected-file disclosure,
  then asks, with a ledger commit,
  `Restore the handoff from before finalizer's interrupted dispatch and run planner to continue from the committed ledger? [y/N]`,
  and without one
  `Restore the handoff from before finalizer's interrupted dispatch and run finalizer again? [y/N]`.
  Any other answer, end of input, no terminal, or `interactive_pause: false`
  discards the record with a `WARN` line and changes nothing. On `y` the
  handoff is restored when it differs and the record is deleted (a failed
  delete stops the run). With a ledger commit the first cycle dispatches the
  planner (routing source `interrupted_finalizer_recovery`) with the
  stale-finalizer instruction, so it continues from the committed ledger
  instead of re-running the ledger update; without one the finalizer runs
  again. Either way that cycle checks protected files against the baseline
  described below.

An audit fan-out (the `auditor` route with the security reviewer on) writes an
`auditor` record to the same path right before it starts. It has the same
shape as a `planner` record (cycle, HEAD, `head_branch`, the `HANDOFF.md` text
the audit starts from, and the protected-file digests; `dispatch` phase only,
no operator files, carried-over files, or separate assignment). The
dispatcher deletes it as soon as the fan-out returns, whatever its exit code
or stop request; when that delete fails it logs an `ERROR` line naming the
record and stops before checking the audit's result. The single-agent
auditor, a resumed handback, and dry runs write none, and the same
one-`WARN`-per-run rule applies when the record cannot be kept. The reviewers
are read-only, so recovering an interrupted audit means restoring the handoff
it started from and running it again. At startup an `auditor` record goes
through the same link, read, validity, and protected-baseline checks as a
`planner` record, then:

- a. Git is compared with the record's HEAD and branch, read-only, in any
  commit mode. When HEAD or the checked-out branch differs, the dispatcher
  logs one `ERROR` line naming the record, both HEADs, and both branches,
  keeps the record, and stops before any dispatch. The dispatcher never resets
  or checks out anything for an auditor record. A dry run logs an `INFO` line
  instead and goes on.
- b. When `HANDOFF.md` still equals the record's text, the dispatcher logs one
  `INFO` line that there is nothing to recover and deletes the record (a dry
  run keeps it). The loop then routes the unchanged handoff to the auditor as
  usual. When the record cannot be deleted, the dispatcher logs one `ERROR`
  line naming it and stops before running the audit, so the stale record
  cannot outlive the audit and be offered for restore at a later start.
- c. Any other handoff (missing, empty, truncated, with a recorded
  `## Dispatcher Verification` section, edited by a reviewer, or a finished
  verdict) is replaced by the record's text only after the operator confirms.
  A dry run logs the summary with `Not applied in dry run.` and keeps the
  record. A handoff path behind a link discards the record with a `WARN` line
  before the prompt. The dispatcher logs the summary, the assignment summary,
  and (with `protected_paths` set) the protected-file disclosure, then asks
  `Restore the handoff and re-run auditor's interrupted <story> audit? [y/N]`.
  Any other answer, end of input, no terminal, or `interactive_pause: false`
  discards the record with a `WARN` line and leaves the handoff as it is. On
  `y` the handoff is restored with the link-safe writer (a link that appears
  in between writes nothing, deletes the record, and logs a `WARN` line) and
  the record is deleted (a failed delete stops the run). The first cycle then
  dispatches the auditor on the restored handoff (routing source
  `interrupted_audit_recovery`) with no forced instruction, checks protected
  files against the baseline described below, and writes a fresh `auditor`
  record. Partial `logs/dispatch/audit/<timestamp>-cycle<N>/` directories
  from the interrupted run are left as they are.

Every startup path above that discards or deletes an in-flight record
(`dispatch`, `handback`, `planner`, `finalizer`, or `auditor`, an invalid
record, a record whose protected-file baseline cannot be checked, a declined
or confirmed recovery, or one with nothing to recover) checks that the delete
worked. When it fails, the dispatcher logs one `ERROR` line naming the record
and the role and story it was read for, keeps the record, and stops before any
dispatch; delete the record, then start the dispatcher again. A confirmed
`dispatch` or `handback` recovery saves its carry-over record only after the
in-flight record is deleted, so this stop saves no carry-over list that a
later start would use without the protected baseline the record holds. A
dry run never deletes the record, so it never stops for this.

A start with no record at all (a run from before the in-flight record
existed, or a record that was lost) has a fallback. It runs only when neither
`logs/dispatch/in-flight.json` nor `logs/dispatch/carry-over.json` existed at
startup and the handoff on disk still routes to `backend` or `frontend` with a
`## Work Packet` the gate can enforce; a start that found either record never
gets a second question. The dispatcher offers the dirty files (the handoff and
`logs/dispatch/` aside) that the Work Packet's Files in scope cover, that do
not match `protected_paths`, and that no commit after the story's last commit
touched, even when a later commit restored the file's content. Merge commits
count too: each is compared with every parent, so a change made only in a
merge still keeps the file out. The story's last commit is the newest of the
last 500 commits on HEAD
whose subject is the `story_id` or starts with it and a space, as the
dispatcher's commit subject does; without a `story_id` or such a commit, HEAD
is used. Any Git failure offers nothing. With nothing to offer it logs
nothing. A dry run logs one `INFO` line naming the role, story, and files,
ending `Not applied in dry run.`, and asks nothing. Otherwise the confirmation
rule above applies: only with `interactive_pause` on and a terminal with
console input does the dispatcher log the offered files, and any dirty files
it did not offer as operator edits, then ask
`Treat these as <role>'s interrupted <story> work? [y/N]`. Every path and
story shown is escaped. On `y` the offered files become that story's
carry-over files for this run, so its next dispatch carries over the ones
still unchanged. Any other answer, end of input, no terminal, or
`interactive_pause: false` logs one `WARN` line and the files stay operator
edits. The fallback writes, stages, and deletes nothing.

A Git process killed mid-write, for example by a second Ctrl+C during a
dispatcher commit (the dispatcher's own `git` calls share the console's
process group), can leave the Git index lock behind, and every later Git
write in the repository fails while it exists. At startup of a real run,
before the publish resume and interrupted-run recovery, the dispatcher asks
Git for the index path (`git rev-parse --git-path index`, which follows
worktrees, a custom `GIT_DIR`, and an inherited `GIT_INDEX_FILE`) and checks
the `<index>.lock` file beside it, where Git creates the lock. When the file
exists it logs a `PAUSE` line starting with `GIT INDEX LOCK:` that names the
escaped path and asks the operator to check that no Git process is running in the repository and then
delete the file. With `poll_interval_seconds` above 0 it polls until the file
is gone, logs `Git index lock removed. Resuming startup.`, and continues; a
stop request during the wait exits with code 0. With `poll_interval_seconds:
0` it exits with code 0 after the `PAUSE` line. A dry run logs one `WARN`
line instead and continues. The dispatcher never deletes, moves, or writes
the lock, and checks it only at startup. When Git cannot report the path,
the check is skipped.

A role can rewrite the record's protected-file digests and pre-dispatch HEAD
too, for example to match a protected file it changed, so with
`protected_paths` set neither recovery kind trusts them. With
`protected_paths: []` none of the following applies. A `dispatch` record
without protected-file digests is discarded with one `WARN` line, as a
`handback` record is. After those checks, a `dispatch` or `handback` record
whose pre-dispatch HEAD is missing, is not a full commit name Git resolves to
a commit, or is neither HEAD nor an ancestor of HEAD
(`git merge-base --is-ancestor`) is discarded before any prompt with one
`WARN` line naming the record, role, story, and reason; nothing is restored,
carried over, or dispatched, and the record is deleted (a dry run keeps it).
Right before either question the dispatcher logs one `INFO` line saying the
record's protected-file baseline cannot be verified and listing, each path
escaped and `none` when empty: the protected files uncommitted now (modified,
staged, deleted, or untracked, the handoff aside; protected files on disk are
also compared with HEAD's tree directly, so one Git ignores, or a tracked one
flagged skip-worktree or assume-unchanged, counts when it differs from HEAD or
is absent from it; the comparison hashes the file's raw bytes, allowing only
CRLF to LF, so no clean filter applies and a filtered file such as one with
the `ident` attribute counts once its bytes differ from HEAD's blob); the
protected files changed
by commits in `<record HEAD>..<HEAD>`; and the protected files changed by
commits on HEAD that no remote-tracking ref has (up to 200 commits), or
`not checked: no remote-tracking refs` when there are none. That last list is
shown only; it does not change the gate. Confirming accepts these protected
files as they are. Once recovery is confirmed, the recovered cycle (the
re-dispatched role for a `dispatch` record, the resumed handback for a
`handback` record) checks protected files against the record's digests and
HEAD with every protected file uncommitted at that moment marked unverified,
so each counts as changed and, outside the Work Packet's Files in scope,
routes the cycle to `user` with `protected_file_modified:`. That includes an
operator's own uncommitted protected-file edit, and an ignored local file under
a protected pattern (for example `.claude/settings.local.json`): commit,
revert, or move it before confirming a recovery you want to continue. A
`dispatch` record that left no agent file, operator change, or handoff change
is deleted as having nothing to recover only when no protected file is
uncommitted either. When a confirmed recovery pauses because operator files
changed, the first dispatch after that pause (in the same run) checks
protected files against the recovery's baseline rather than a new snapshot.
A `planner` record follows the same rules, and its recovered cycle is the
re-dispatched planner; unlike a `dispatch` record, an unchanged handoff
deletes it as having nothing to recover whether or not a protected file is
uncommitted, because nothing from the record is applied.

The one-shot handoff repair follows the same rule: in dispatcher mode its
prompt says to leave the handoff uncommitted, and if the repair commits the
handoff anyway, the dispatcher undoes that commit with `git reset --soft`, the
same way it does for the planner.

Use it when an implementer runs in a sandbox that keeps `.git` read-only (Codex
`workspace-write`), or simply to keep agents out of Git history.

### Publishing: Story Branches And Pull Requests

```yaml
commit_mode: dispatcher
publish:
  mode: local            # local (default) | merge-commit | pr
  base_branch: null      # default: the branch checked out when a story starts
  branch_prefix: llm-handoff/
  remote: origin         # pr mode
  auto_merge: true       # pr mode: merge once the audit passes
  wait_for_checks: false # pr mode: wait for the PR's CI checks before merging
  wait_for_reviewers: [] # pr mode + auto_merge: [codex] waits for Codex review
  review_timeout_seconds: 1200
  review_poll_seconds: 30
  review_stall_seconds: 600 # a review shown running this long is stalled
  review_stall_action: prompt # prompt | merge, after the retry stalls too
  max_review_rounds: 1 # >1 needs Codex to review every push on its own
  codex_bot_login: chatgpt-codex-connector[bot]
  resolve_project_state_conflicts: planner # pr mode: planner | "off"
```

- `local`: commits stay on the branch you have checked out.
- `merge-commit`: before an implementer's first dispatch for a story, the
  dispatcher creates `llm-handoff/<story-id>-<title>` from the base branch.
  After the audit passes, it merges that branch into the base with `--no-ff`.
  The merge commit carries a report for people to read, built from data the
  loop already has. It has two sections, and the dispatcher lays it out
  itself, so the format holds whatever the agents wrote:
  - Summary: at most three sentences (about 500 characters) from the start
    of the Work Packet's Objective. A sentence that ends with a colon, such
    as "Acceptance checks:", starts the details instead.
  - Reviewer notes: `### Story` (story, epic, implementer), then
    `### Acceptance checks` (the rest of the Objective, with its lists and
    line breaks kept), then `### Audit` (both reviewers' verdicts, with the
    full reviews folded).

  The report is not part of the dispatcher's checks, and it leaves out what
  other places already show: changed files and commits (the pull request's
  Files and Commits tabs, or `git log`), the Work Packet and the gate's
  verification results (`HANDOFF.md`), and token usage (the dispatch log).
  Nothing is pushed.
- `pr`: same story branch. After the audit passes, the dispatcher pushes it,
  opens a pull request with that report as the body (`gh pr create`), and
  with `auto_merge: true` merges it (`gh pr merge --merge --delete-branch`)
  and continues on the updated base branch. It only auto-merges when every
  review thread on the pull request is resolved, including threads from
  people; otherwise the loop stops and leaves the pull request open. With
  `auto_merge: false` the loop stops after opening the pull request so you
  can review and merge it. With
  `wait_for_checks: true` it waits for the pull request's checks
  (`gh pr checks --watch`) before merging and stops if they fail; only enable
  this when the repository has CI.

#### Keeping The Story Branch Up To Date

The base branch can move while a story is in progress, for example when
another pull request merges. In `pr` mode, before every push of the story
branch (the first push and each correction round), the dispatcher runs
`git fetch <remote> <base_branch>` and merges `<remote>/<base_branch>` into the
story branch (`git merge --no-edit`). The review then sees the merged result.
If `gh pr merge` later reports that the pull request is not mergeable,
because the base branch moved again during the review, the dispatcher does
the same merge once more, pushes it, and retries the merge once.

When that merge conflicts, the dispatcher runs `git merge --abort` and pauses
(exit code 0). It names the conflicting files and keeps the story state in
`logs/dispatch/publish/story.json`. To resume, on the story branch run
`git merge <remote>/<base_branch>`, resolve the conflicts, and commit the
merge, then start the dispatcher again. At startup it pushes the story
branch and publishes it as usual. If it paused at the merge, your merge
commit lands on the open pull request. Codex reviews new pull requests, not
later pushes, and the dispatcher asks for a review only to retry a failed
one. So with
`wait_for_reviewers: [codex]` the dispatcher pushes that commit and pauses,
leaving the pull request open for you to review and merge; without it, the
dispatcher merges the pull request. If the retried
`gh pr merge` fails again, the dispatcher pauses the same way and leaves the
pull request open. The next start retries the merge, or you can merge it
yourself.

When `project_state_path` is the only conflicted file, the dispatcher first
asks the planner to resolve it (`resolve_project_state_conflicts: planner`,
the default). This covers the common case of an operator editing the backlog
in `PROJECT_STATE.md` while a story that also updates its own status there is
running. The planner gets one bounded prompt, not its handoff prompt: keep
both sides (the operator's base-branch edits and the story's own status
edits), take the base-branch version of any epic the story did not touch,
remove every conflict marker, edit only that file, and do not stage or
commit. The dispatcher then checks the result itself:

- the planner exited with code 0 and left the handoff and every protected
  control file unchanged (a changed handoff is put back byte for byte, but
  only when no symlink or junction is in its path; otherwise nothing is
  written and a warning names the handoff);
- HEAD did not move and the merge is still in progress;
- no file other than `project_state_path` changed, compared with the state
  right after the merge stopped: every tracked path from `git ls-files` is
  compared by `git status` entry, file type (regular file, executable,
  symlink, directory, or missing, read without following links), and content,
  whatever its `assume-unchanged` or `skip-worktree` flag, along with every
  untracked path `git status` lists. A tracked submodule is compared by its
  HEAD and the same per-path snapshot of its own checkout, so a submodule
  edit hidden by submodule ignore settings is caught; a submodule directory
  that has content but no `.git`, or that Git cannot read, fails the check.
  A tracked or listed path whose type, content, or link target cannot be
  read, before or after the planner runs, also fails the check, so a file
  unreadable in both snapshots cannot hide a change;
- no index flag changed on any path, `project_state_path` included: an
  `assume-unchanged` or `skip-worktree` flag set, cleared, or added for a new
  entry (from `git ls-files -v`) is rejected;
- no index entry other than those of `project_state_path` changed: every
  entry's mode, blob ID, and stage from `git ls-files --stage` is compared,
  so a staged change hidden by `assume-unchanged` or `skip-worktree` is
  caught;
- the file exists, is not empty, and has no conflict marker line: a line
  that starts with a run of `<`, `|`, or `>` at least the marker size long,
  or a line of exactly that many `=`. The marker size is 7 unless the file's
  `conflict-marker-size` attribute in `.gitattributes` sets another.

When every check passes, the dispatcher stages the file, checks that the
index differs from the one right after the merge stopped only in that file
(now one regular-file entry at stage 0), and commits the merge
(`git commit --no-edit`), so the merge commit holds only the conflict
resolution, logs `Planner resolved a PROJECT_STATE.md merge conflict` with the
merge commit, and continues publishing. The pull request records it for
review under `### Merge conflict resolution` with a
`git show <merge> -- PROJECT_STATE.md` command, which shows the lines that
differ from both sides. A resolution before the push goes in the pull request
body; one at the retried `gh pr merge` goes in a pull request comment, because
the body is already written, and that merge goes ahead without another Codex
review, like the clean base-branch merges there.

Any failed check, a planner failure, a conflict in any other file, or a second
conflict in the same publish runs `git merge --abort` and pauses as described
above; the pause message says why the planner's resolution was not used.
Files the planner changed beyond `project_state_path` are left as they are for
you to review. The planner runs at most once per publish and never in
`--dry-run`. `resolve_project_state_conflicts: "off"` (quoted; YAML reads a
bare off as false) always pauses. The check for markers is textual: a file
that uses a line of exactly the marker size in `=` characters (seven by
default, such as a Markdown setext heading underline of that length) always
fails it and pauses.

Uncommitted files are never stashed, discarded, or committed by the
dispatcher. When the checkout has uncommitted files (staged, unstaged, or
untracked), the dispatcher first checks whether the merge would change any of
them, using `git merge-tree --write-tree`, which runs any configured merge
drivers, as the resume check below does. If it would, the dispatcher does not
merge and pauses (exit code 0) with the story state kept, naming those
files. Git also refuses a merge whenever changes are staged; that pause names
the files from git's `error:` line. Neither pause asks you to resolve conflicts, because there are
none: set the named files aside without committing them to the story branch,
for example with `git stash push --include-untracked -- <files>` or a commit
on another branch, then start the dispatcher again. It merges the base branch
itself, then continues where it paused. A commit added to the story branch
has not passed the audit, so the resume refuses it. Any other failed merge
pauses the same way and names git's `error:` or `fatal:` line rather than an
earlier `warning:` line such as a line-ending notice.

Before a resume pushes anything, the dispatcher fetches the base branch and
checks what was added to the story branch since the pause:

- Only merge commits, or commits already on the base branch, were added. Any
  other commit has passed neither the work-claim gate nor the audit.
- Each added merge commit is a two-parent merge whose second parent is on
  `<remote>/<base_branch>`, and it holds only the conflict resolution: its
  tree differs from the automatic merge of its parents
  (`git merge-tree --write-tree`, git 2.38 or later) only in files that the
  automatic merge left conflicted. Parent order matters: run
  `git merge <remote>/<base_branch>` while on the story branch. A merge made
  the other way round, with the base branch as its first parent, is refused.

If either check fails, or a `git` command it runs fails (including an older
git without `merge-tree --write-tree`), the dispatcher pauses again with the
story state kept, pushes nothing, and names the offending commits or files.
Redo the merge so it holds only the conflict resolution, or publish the
branch yourself. The conflicted files themselves are not inspected beyond
that: an edit inside a conflicted file that goes past the conflict
resolution is not detected, so review those files before you start the
dispatcher again. When the pause names files changed beyond the conflict
resolution, it also names the conflicted files the merge changed, for that
review.

The check compares your merge with what `git merge-tree` produces for the
same two parents, so merge drivers matter. `git merge-tree` runs the merge
drivers configured for the repository (`.gitattributes` plus
`merge.<driver>.driver` in Git config) during the resume check, so any custom
merge driver command runs on dispatcher start when a paused publish resumes.
A merge driver set in `.gitattributes`, such as the built-in `union` or a
custom one, can also make the resume pause: if it
resolves a file in `git merge-tree` that your merge resolved differently, or
the reverse, the two disagree on that file and the check reports it. To
continue, redo the merge or publish the branch yourself.

Many stories add a bullet at the top of the same changelog section, which
conflicts whenever two of them are open at once. A line in the target
repository's `.gitattributes` lets the local merge keep both sides:

```gitattributes
CHANGELOG.md merge=union
```

A union merge keeps every line from both sides without checking that the
result makes sense, and a blank line that both sides added can appear only
once, so only use it for files where that is safe. `init` does
not add it: the reference workflow does not ship a changelog, and
`.gitattributes` belongs to the target repository.

#### Resuming An Interrupted Publish

A `pr` publish can be cut off part way, for example by a second Ctrl+C, a
closed window, or a crash. The next start then finds the story state in
`logs/dispatch/publish/story.json`, the checkout on the story branch, and
`HANDOFF.md` still the story's `verified_pass`. Right after the paused-publish
resume above, and only outside `--dry-run`, the dispatcher checks that state.
It acts only when the story state has no paused publish and no pending Codex
review retry, the story branch is checked out, and the handoff has the saved
story's `story_id`, `status: verified_pass`, `close_type` `story` or `epic`,
and a `scope_sha` that resolves to a commit. It then pauses (exit code 0,
story state kept, a message starting `INTERRUPTED PUBLISH:` with the manual
steps) when:

- a merge is in progress, or a file other than the handoff and
  `logs/dispatch/` is uncommitted;
- HEAD is not the audited `scope_sha` plus only clean base-branch merges;
- `gh pr view` fails for any reason other than "no pull requests found";
- the pull request is closed (the dispatcher never reopens or recreates it);
- the pull request is merged at a head other than the local HEAD, for
  example when it was merged while the dispatcher was down and an audited
  correction was never pushed to it;
- the pull request is merged with an unresolved review thread or saved
  finding;
- the story's saved review findings were made on HEAD and are not resolved:
  the review-bounce handoff to the implementer was lost, and publishing again
  would count that correction round twice.

A merged pull request with every thread resolved is cleaned up without any
push or `gh` write: the dispatcher switches to the base branch, runs
`git pull --ff-only`, deletes the merged local story branch, and clears the
story state. For an open pull request, or none yet, it logs the branch, HEAD,
the pull request, and what it will do, then asks
`Resume publishing <story_id> on <branch>? [y/N]` at the dispatch terminal.
`y` publishes again as usual: it merges the base branch, pushes, reuses the
open pull request or opens one, waits for the review, and merges. Any other
answer, or no interactive terminal (`interactive_pause: false` or redirected
input), logs a `WARN` line, keeps the story state, pushes nothing, and the
loop continues as before.

`story.json` records the head commit the dispatcher last pushed for review
and when (`publish_requested_head`, `publish_requested_at`). A publish of the
same head after a restart waits for Codex signals since that time, so a
review or thumbs-up Codex gave while the dispatcher was down still counts. A
new head, such as a correction round or a new base-branch merge, gets a new
time.

In `merge-commit` mode the publish runs `git switch <base>`,
`git merge --no-ff <branch>`, and `git branch -d <branch>`, then clears the
story state. At startup, outside `--dry-run`, the dispatcher applies the same
entry check (saved story state, and `HANDOFF.md` still that story's
`verified_pass` with a resolving `scope_sha`, the audited commit), then reads
the checkout. On the story branch it pauses when a merge is in progress, a
file other than the handoff and `logs/dispatch/` is uncommitted, or HEAD is
not exactly the audited commit; otherwise it asks
`Resume merging <story_id> into <base>? [y/N]`, and `y` runs the normal
merge-commit publish (any other answer, or no terminal, logs a `WARN` line and
keeps the story state). On the base branch it never merges, resets, or asks:
it pauses when a merge is in progress, when the story branch's tip is not the
audited commit, or when the audited commit is not on HEAD; when the merge
already went through, it deletes the merged story branch (if it still exists)
and clears the story state. On any other branch, or a detached HEAD, it does
nothing. Each pause starts with `INTERRUPTED PUBLISH:`, keeps the story state,
and lists the manual steps: `git switch <base>`, `git merge --no-ff <branch>`,
`git branch -d <branch>`, delete `logs/dispatch/publish/story.json`, then
start the dispatcher again.

#### Waiting For The Codex Pull Request Review

With `wait_for_reviewers: [codex]`, `pr` mode with `auto_merge: true` waits
for the Codex code review (the ChatGPT Codex connector for GitHub) of the
exact commit it pushed before merging. Set this only when Codex reviews pull
requests in the repository on its own: the dispatcher asks for a review only
to retry one that Codex reports as failed. The dispatcher polls the pull request with
`gh api` every `review_poll_seconds`. It counts only activity from
`codex_bot_login`, and only for the pushed commit's SHA:

- The review is finished when the Codex summary comment's `Code Review` row
  shows `Completed` for that commit, when Codex posts a pull request review
  on that commit, or when Codex adds a +1 reaction after the push.
- Inline review comments on that commit are findings.

The outcomes:

- No findings: the dispatcher merges the pull request as usual.
- Findings: the pull request stays open and is not merged. The dispatcher
  rewrites `HANDOFF.md` as a `verified_fail` bounce to the story's implementer.
  The bounce lists each finding (file, line, text, link) and carries the Work
  Packet. The implementer fixes the findings on the same story branch and
  writes a `## Review Responses` section with one `### Finding N` entry per
  finding: what it changed, or why it left the finding. The dispatcher saves
  that section when the gate passes and again before it dispatches the
  auditor, because the auditor rewrites `HANDOFF.md`. The second save keeps
  the responses when the claim reaches the auditor without a gate pass, for
  example after a dispatcher restart or a handoff fixed by hand after a
  validation pause. Saved responses last for the whole correction round: a
  later claim in the round, such as the implementer's handback after an
  audit bounce, merges its responses into the saved ones by finding number
  (its response replaces the saved one for the same finding), and a claim
  without responses keeps the saved ones. A new correction round starts with
  none. After the gate and audit pass again, the dispatcher pushes to the
  same pull request and updates its report. It posts each response as the
  reply on that finding's review thread and resolves the thread. A finding
  without a response keeps its thread unresolved, gets no reply, and logs a
  `WARN`; it goes to the planner's pass below with the deferred findings. A
  response that starts with `Deferred:` (or `**Deferred:**`) marks
  a real finding left unfixed for later work, often because its fix is
  outside the Work Packet's Files in scope: the dispatcher posts the reply,
  logs a `WARN` naming the finding, and leaves the thread unresolved.
  Deferring every finding is still a `ready_for_review` handback with no code
  change: the dispatcher records the branch head as the claim, and the
  work-claim gate counts the story's earlier commits since the story base. A
  `blocked_*` handback skips the gate, the audit, and the replies, so the
  planner's pass below would never start; the bounce says so. As a
  deterministic backup, a correction-round handback under any other status
  counts as a deferral when its `## Review Responses` answer every open
  finding with `Deferred:` and it changed no story file (no new commit and
  nothing uncommitted in Files in scope): the dispatcher posts the replies,
  leaves the threads unresolved, and routes to the planner's pass. Mixed
  responses or any change leave the handback as written.
- Deferred findings only: when the only unresolved review threads are
  deferred findings or the story's saved findings left without a response,
  the dispatcher does not pause and keeps the story state. It rewrites
  `HANDOFF.md` to route to `planner` with each such finding, the
  implementer's response (or a note that none was saved), and the current
  Work Packet. The planner gets one pass per pull
  request and decides each finding:
  - Widen the scope: route back to the implementer with the same
    `story_id` and a Work Packet whose Files in scope cover the fix. The
    implementer answers under `## Review Responses` as usual, and the fix
    merges without another review once every thread is resolved.
  - Accept it as follow-up work: plan a follow-up story and add a
    `## Deferred Finding Decisions` section with a `### Finding N` entry that
    starts with `Follow-up:` and names it. After the planner's cycle passes
    validation, the dispatcher posts each decision as the reply, resolves
    the thread, and merges once every review thread is resolved.
  - Ask the user: route to `user`. The pull request stays open.

  The dispatcher pauses for a person when the planner leaves a finding
  neither accepted nor sent back, when the implementer defers a finding
  again after the planner's pass, or when any other review thread, such as
  a person's comment, is still unresolved.

  A planner handoff can also carry `## Deferred Finding Decisions` when that
  pass never started, for example after a blocked handback. The dispatcher
  acts on it only when that is provably safe: every unresolved finding has
  a decision, the checkout is on the story branch, and the pull request
  head is the commit the Codex review reviewed. That head may also carry
  clean base-branch merges, whose tree must equal the automatic merge.
  Otherwise it fails closed and pauses with the reason and the recovery
  steps; it posts, resolves, and merges nothing, and it keeps the story
  state. A leftover section with no unresolved findings is ignored. The
  story state records the reviewed commit as `reviewed_head`. For a state
  saved by an older version, the dispatcher reads the commit the open
  findings were made on from GitHub.
- A handoff that moves on while a story is unpublished: before each cycle
  that is not a planner dispatch, `pr` mode checks whether `HANDOFF.md`
  moves on from the saved story. That means it names another story, routes
  to `finalizer`, or records the story or epic as `verified_pass`. If it
  does, the saved story's pull request must be merged on GitHub with every
  review thread resolved. A merged story's state is cleared, the checkout
  returns to the updated base branch, and the cycle goes on. An open pull
  request is recovered without a person when all of these hold:
  - only the story's saved findings are unresolved;
  - the planner has not had its pass;
  - the checkout is on the story branch at the pull request head;
  - that head is the reviewed commit, plus only clean base-branch merges;
  - no file in scope is uncommitted.

  The dispatcher then saves `HANDOFF.md` to
  `logs/dispatch/publish/displaced-handoff.md` and routes to the planner's
  deferred-findings pass. The handoff tells the planner to restore the saved
  route after deciding. Every other case pauses with the pull request, its
  unresolved findings, and the two recovery paths: finish the correction
  round, or resolve the threads and merge by hand. That includes a `gh` or
  `git` failure, a moved or unreviewed head, a person's comment, and
  findings still open after the planner's pass. Pause recovery and the
  operator prompt apply. A handoff that names the saved story itself passes
  the check, and so does any handoff in `local` mode or when there is no
  saved story. `--dry-run` makes no `gh` calls. Instead it reports a local
  pause when an implementer would start another story on the story branch.
- One correction round by default (`max_review_rounds: 1`). The first review
  should surface what matters most; each review of a fix tends to find new
  edge cases in the new code, so the dispatcher does not wait for another
  review after the last correction round. It merges once every review thread
  is resolved. With a higher `max_review_rounds`, it waits for the review
  Codex runs on the pushed fix and can bounce again, up to that many rounds.
  That only works when Codex is set to review every push to a pull request,
  not just newly opened ones: the dispatcher does not request a review of a
  pushed fix, so without per-push reviews the wait times out (see below).
- A failed review (the summary's `Code Review` row shows `Failed`): the
  dispatcher posts one pull request comment, `@codex review`, and waits for
  the same head commit again with the same `review_timeout_seconds`. It
  records the retry as `review_retry_head` and `review_retry_at` in
  `logs/dispatch/publish/story.json` and never asks again for the same head.
  If the dispatcher stops during that wait, the next start resumes it when
  the checkout is still on the story branch at that commit and `HANDOFF.md`
  is still the story's `verified_pass`. A summary comment Codex has not
  updated since the request (or updated in the same second) does not count.
  The comment never goes in the pull request body.
  A failure Codex attributes to its usage limit for code reviews is not
  retried automatically.
- A stalled review: the summary keeps showing the head commit's review
  running (often with only an eyes reaction) for `review_stall_seconds`
  (default 600), counted from the first poll of the current wait that sees it
  running. That ends the wait early and is handled like a failed review: one
  `@codex review` comment, recorded the same way. So is a wait that reaches
  `review_timeout_seconds` with no completed review. A restart at the same
  head never posts a second automatic request.
- A completed review that names no commit (the summary's state marker has
  `commit_sha: null` and its `Commit` cell is a dash, with no review or +1
  reaction from Codex): nothing ties that review to the head commit, so it
  is not a pass. When a summary updated after the push or request shows it
  on two polls in a row, the wait ends and is handled like a failed review:
  one `@codex review` comment, then `review_stall_action` below.
- When the retried review also stalls, times out, fails, or names no commit,
  `review_stall_action` decides:
  - `prompt` (the default) uses the operator prompt below.
  - `merge` merges the pull request without a Codex review, but only when
    `HANDOFF.md` is the story's `verified_pass` with a `scope_sha` that is
    the head commit (or the head adds only clean base-branch merges to it)
    and no review thread is unresolved. Before merging, the dispatcher logs
    a `WARN` and posts a pull request comment that starts with "Merging
    without a Codex review:" and gives the reason. When a condition fails,
    it logs a `WARN` with the reason and falls back to `prompt`. If the base
    branch moved and the pull request must take a base-branch merge first,
    only a clean merge goes ahead: a conflict pauses, and the planner is not
    asked to resolve it, so the merged commit stays the audited one plus
    clean base-branch merges.

  A completed review with findings still takes the correction round, and a
  usage-limit failure always goes to the operator prompt, whatever the
  action.
- When the stall action is `prompt` or its merge was refused, the account
  hit its usage limit, or asking Codex again failed, the dispatcher pauses at
  a short operator prompt if `interactive_pause` is on and stdin is a
  terminal:
  - `merge` merges the pull request without the Codex review. The
    dispatcher logs a `WARN` and, before merging, posts a pull request
    comment recording the operator's decision to merge without a Codex review
    and why. Every review thread must still be resolved.
  - `retry` posts one more `@codex review` comment and waits again.
  - `quit` stops and leaves the pull request open, as below.

  Any other input repeats the choices; closed input or `llm-handoff stop`
  counts as `quit`. Without a terminal (or with `--no-interactive-pause`),
  the loop stops and leaves the pull request open, and so does a `gh`
  error. Review it and merge it yourself, run `git switch <base_branch>` and
  `git pull`, then start the dispatcher again. The dispatcher only merges
  without a finished review when the operator types `merge` or
  `review_stall_action` is `merge`.
- A completed review with findings never gets a review request: findings
  always take the correction round above.

The pull request report has every `@name` mention neutralized (a word
joiner after `@`), because GitHub treats a mention of `@codex` anywhere in
the body, even in a code span, as a request for a Codex cloud task rather
than a code review.

Finding text comes from the pull request and is untrusted. The bounce tells
the implementer to check each finding and to fix only what holds up inside
the Work Packet's scope. The work-claim gate and the audit still apply to
the fix.

Agents never branch, push, or merge: the dispatcher does all of it, and only
after the work-claim gate and both reviewers pass. `pr` mode pushes with your
Git credentials and needs the GitHub CLI (`gh`) signed in. Both branch modes
require `commit_mode: dispatcher`. A failed fetch, push, merge, or `gh` call
stops the loop with the error instead of retrying. The exceptions are a
conflicting base branch merge, which pauses, and a pull request that is not
mergeable, which is retried once after a base branch merge.

A passing audit with no story branch also stops the loop, because the work
would be left unpublished. The exception is an epic close
(`close_type: epic`): the planner sends it to the auditor after the epic's
last story merged, so the checkout is the base branch and there is nothing to
publish. The exception needs the checkout to be `base_branch` (or, when
`base_branch` is null, a branch without `branch_prefix`). In `pr` mode it also
needs every commit on that branch to be on `<remote>` already. Otherwise the
loop still stops.

An epic close can also pass on a story branch with no work of its own, for
example one cut when a failed close audit was routed to an implementer that
had nothing to change. In `pr` mode, when the branch has no commits beyond
`<remote>/<base_branch>` after the base branch merge, the dispatcher pushes
nothing and opens no pull request. It fast-forwards the base branch to that
commit, switches to it, and deletes the story branch and its story state.

A story no agent role can do, such as edits under `.claude/`, goes to `user`:
the operator commits the work on the base branch without pushing, and the
planner sends that commit to the auditor with `close_type: story` and
`scope_sha` set to it. In `pr` mode, when that audit passes with no story
state, the dispatcher publishes the commit as a story. It needs
`base_branch` and `branch_prefix` set, the checkout on `base_branch`, HEAD
equal to the handoff's `scope_sha` and exactly one non-merge commit ahead of
`<remote>/<base_branch>` (the audit without story state reviews only
`HEAD~1..HEAD`), that commit not changing `HANDOFF.md`, nothing else
uncommitted, and no existing branch with the story's name. It then creates the story branch at HEAD (named from the
handoff's `story_id` and `story_title`) with its story state, checks it out,
and moves the local base branch back to `<remote>/<base_branch>` without
touching the working tree. The publish continues as usual: push, pull
request, review gate, and merge. The pull request report names the operator
as the implementer, and a review bounce goes back to `user` with
`status: escalate_to_user`, so the loop pauses for the operator. When any
check fails, a `WARN` line names it and the loop stops as before.

### Scanners

The gate can also run operator-configured scanners before the verification
command. The list is empty by default, so nothing changes until you add
entries, and scanners only run while `enforce_work_packet` is `true`. A
scanner runs only when it is listed under `scanners:` and its executable is on
`PATH`. Each entry names a tool the target repository has installed;
`llm-handoff` does not bundle or install any of them.

`llm-handoff doctor` reports each configured scanner and whether the gate will
run it: `PASS` when its executable is on `PATH`, `FAIL` when a
`required: true` scanner's executable is missing (every claim the scanner
applies to would fail with `scanner_missing:<name>`), and `WARN` when a
`required: false` scanner's executable is missing, when the command does not
start with a plain program name doctor can look up, or when scanners are
configured but the gate is off. When the command uses `{changed_files}` or
`{changed_py_files}`, the gate skips the scanner on claims where that list is
empty, and doctor's message says the outcome applies only to claims where the
list is nonempty. Doctor looks the executable up the same way the gate does
and never runs a scanner command.

For Python repositories, `bandit`, `pip-audit`, and `gitleaks` are the
recommended starting set. Use `semgrep` only with a pinned local ruleset kept
in the repository (`--config <local ruleset path>`), not `--config auto`,
which downloads whatever rules the registry serves at run time. Pin each
scanner's version in the target repository's tool setup, so the gate's results
do not change when a scanner updates itself.

```yaml
enforce_work_packet: true
scanners:
  - name: gitleaks
    command: gitleaks git --no-banner --redact --log-opts="{diff_range}" {repo_root}
    timeout_ms: 300000
    required: true
  - name: semgrep
    # <local ruleset path> is a ruleset file committed to the repository,
    # for example .semgrep/rules.yml.
    command: semgrep scan --config <local ruleset path> --metrics off --error --quiet {changed_files}
    timeout_ms: 300000
    required: true
  - name: bandit
    command: bandit -q {changed_py_files}
    timeout_ms: 120000
    required: true
  - name: pip-audit
    command: pip-audit --strict
    timeout_ms: 300000
    required: false
```

On gitleaks releases before 8.19 use
`gitleaks detect --source {repo_root} --log-opts="{diff_range}"` instead of the
`git` subcommand. `pip-audit` needs network access to query its vulnerability
database. `semgrep` with a local ruleset and `--metrics off` does not need to
download rules; `--config auto` would, and its results could change between
runs without a commit.

Fields:

- `name`: label used in log lines and error prefixes; letters, digits, `.`,
  `_`, and `-`, unique within the config.
- `command`: shell text run in `repo_root` after placeholder substitution.
- `timeout_ms`: per-scanner bound, default `300000`; the process tree is
  killed on timeout.
- `required`: default `true`, so a missing executable fails the claim. With
  `false`, a missing executable is logged at WARN level and the scanner is
  skipped.

Placeholders:

| Placeholder | Value |
| --- | --- |
| `{diff_range}` | `<pre-dispatch HEAD>..<scope_sha>`, ready for `git log` or `git diff` options |
| `{changed_files}` | space-separated, shell-quoted repository-relative paths changed in that range that still exist |
| `{changed_py_files}` | the `.py` subset of `{changed_files}` |
| `{repo_root}` | the shell-quoted absolute repository root |

Scope gitleaks, semgrep, and bandit to the diff or the changed files as shown.
Run pip-audit unscoped, because a dependency audit is not diff-shaped. Literal
braces in a command must be doubled (`{{` and `}}`). A command that references
an unknown placeholder fails config load with `scanner_config_invalid:<name>`.
A scanner whose command references `{changed_files}` or `{changed_py_files}`
is skipped with an INFO log line when that list is empty for the claim.

Scanners run only after the `scope_sha`, diff-scope, and working-tree checks
pass; there is no value in scanning out-of-bounds work. They run in config
order before the verification command, and every scanner runs even after one
fails, so a single bounce reports every finding:

| Outcome | Error prefix | Effect |
| --- | --- | --- |
| non-zero exit | `scanner_failed:<name>` | claim bounces to the implementer with a bounded output tail |
| executable not found, `required: true` | `scanner_missing:<name>` | claim bounces to the implementer |
| executable not found, `required: false` | none | WARN log line, scanner skipped |
| timeout | `scanner_timeout:<name>` | process tree killed, claim bounces to the implementer |
| placeholder cannot be filled | `scanner_config_invalid:<name>` | rejected at config load; if it reaches runtime the claim escalates to `user` |

Scanner commands are operator-authored configuration, so they are trusted
operator input, unlike the Work Packet verification command, which is
agent-written. The dispatcher does not sandbox scanners any more than it
sandboxes agents. Scanners get the same allowlisted environment as the
Verification command; list any variable a scanner needs, such as a
`SEMGREP_APP_TOKEN` or a proxy setting, under `work_claim_env_passthrough`.

## Agent Environment

Each provider CLI is launched with an explicit environment built from an
allowlist. The dispatcher's own environment is not copied, so tokens such as
`GITHUB_TOKEN`, `AWS_*` credentials, or database passwords in the shell that
started the dispatcher do not reach the agents.

In the default `env_mode: allowlist`, a launch receives only:

- process basics: `PATH`, `HOME`, `USERPROFILE`, `SystemRoot`, `SystemDrive`,
  `TEMP`, `TMP`, `LANG`, `LC_ALL`, `TERM`, and the account name (`USERNAME`,
  `USER`, `LOGNAME`), which tools such as pytest use for per-user directories;
- non-secret process plumbing the CLIs need to start: `COMSPEC`, `PATHEXT`,
  `WINDIR`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, `SHELL`;
- the provider's own configuration variables: `CLAUDE_*` for Claude Code and
  `CODEX_*` for Codex. The Antigravity CLI (`agy`) gets no provider
  variables: it reuses the sign-in stored under the
  user profile, which `HOME`, `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA`
  already reach;
- any extra names listed under the role's `env_passthrough`.

Names are matched case-insensitively.

Vendor API keys follow a fixed rule in every mode. A key only reaches its own
provider, and only when the role opts in:

| Key | Passed to |
| --- | --- |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | never; Claude Code uses its own login |
| `OPENAI_API_KEY` | Codex roles with `use_api_key_env: true` |
| `GOOGLE_API_KEY` | never |

No role receives `GEMINI_API_KEY`: every launch strips it, and
`env_passthrough` rejects it.

Antigravity roles receive no vendor API key, even with `use_api_key_env: true`.

`env_passthrough` cannot name a vendor API key; config loading fails if it
does.

```yaml
agents:
  backend:
    provider: codex
    env_passthrough: [HTTPS_PROXY, SSL_CERT_FILE]
```

`env_mode: inherit` restores the previous behavior of copying the
dispatcher's environment into the launch. The vendor API key rule above still
applies. Use it only for debugging a CLI that needs a variable you have not
identified yet; prefer adding that name to `env_passthrough`.

Codex roles previously inherited `OPENAI_API_KEY`. A Codex setup that
authenticates with an API key instead of `codex login` now needs
`use_api_key_env: true` on that role.

## Launch Permissions And Sandboxing

Each role launches its provider CLI with the safer arguments below. A role can
restore the previous approval and sandbox bypass with `unsafe_bypass: true`.
The startup banner prints one `Launch <role>:` line per role and a
`WARN UNSAFE BYPASS:` line for every role that opts out.

| Provider | Default launch | `unsafe_bypass: true` |
| --- | --- | --- |
| Claude Code | `--permission-mode dontAsk --tools <tool names> --strict-mcp-config --allowedTools <allowed_tools> --disallowedTools WebFetch,WebSearch` | `--dangerously-skip-permissions` |
| Codex | `--sandbox workspace-write --ask-for-approval never` | `--yolo` |
| Antigravity (`agy`) | none | `--dangerously-skip-permissions` |

Claude Code in `dontAsk` mode denies any tool call that `allowed_tools` does
not allow instead of prompting. `--allowedTools` only pre-approves rules, so
the launch also passes `--tools` with the base tool names from the list
(`Read`, `Bash`, and so on). Built-in tools outside the list are then not
available at all, even if user or project settings approve them.
`--strict-mcp-config` loads no MCP servers unless `allowed_tools` names an
`mcp__` tool. Linear mode with `tracker.claude_mcp_config` set also passes
`--mcp-config <path> --strict-mcp-config` (see
[Tracker](#tracker-optional-linear-mode)). The default list is:

```yaml
allowed_tools:
  - Read
  - Edit
  - Write
  - Grep
  - Glob
  - Agent
  - Bash(git status:*)
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git show:*)
  - Bash(git cat-file:*)
  - Bash(git add:*)
  - Bash(git commit:*)
  - Bash(git rev-parse:*)
```

With `interpreter_grants: true` (see [Interpreter Grants](#interpreter-grants))
the default list also ends with `Bash(python:*)`, `Bash(python3:*)`, and
`Bash(pytest:*)`, in that order, which is the default list before that key
existed.

`Agent` is included because the Claude-side prompts delegate to the named
subagent, and `git cat-file` because the shipped `handoff-validator` resolves
SHAs with it. Setting `allowed_tools` on a role replaces the whole list, and
`interpreter_grants` does not change a list a role sets itself. The list is a
guardrail, not a sandbox: any interpreter rule, such as `Bash(python:*)`, lets
the agent run any command, `git push` included. `git push` is not on the
list. `llm-handoff doctor` reports interpreter rules per role as
`interpreter grants` warnings.

Claude Code also protects some paths, and `dontAsk` mode denies every write
to them before it reads `allowed_tools` or settings allow rules. One of them
is any `.claude` directory, at the repository root or nested such as
`examples/app/.claude/agents/x.md`; see Claude Code's
[protected paths](https://code.claude.com/docs/en/permission-modes#protected-paths)
for the full list. A `provider: claude` implementer launched this way cannot
change those files, so before it dispatches `backend` or `frontend` the
dispatcher checks the Work Packet. When an entry in `Files in scope` has a
`.claude` path segment (any case) and the role is `provider: claude` without
`unsafe_bypass: true`, it pauses with a `CLAUDE PROTECTED PATHS IN SCOPE`
reason that names the entries, instead of launching a role that would hand
back `blocked_missing_context`. With `pause_recovery: planner` the planner
gets one attempt to rewrite the packet first. To resolve it, apply those
edits yourself and drop them from the packet, or have the planner give them
to a role whose provider is not `claude` (the `.agents/` and `.codex/`
folders are not protected). An `@backend` or `@frontend` dispatch from the
operator prompt skips the check. `--dry-run` logs the pause it would take.
The check leaves out the writable exceptions Claude Code documents under the
repository root's `.claude` folder: its git worktrees under
`.claude/worktrees/`, and Markdown entries (ending in `.md`) under the
subagent memory folders `.claude/agent-memory/` and
`.claude/agent-memory-local/`. A directory entry such as
`.claude/agent-memory/` still pauses, because it can cover other files.
Only `.claude` is checked; a packet that scopes another protected path, such
as `.git/` or `.vscode/`, still dispatches and the writes are refused.

`llm-handoff init` reads the target repository's CI workflows,
`package.json`, `pyproject.toml`, `Makefile`, `Cargo.toml`, and `go.mod`,
and prints the build and test commands it finds, each with the file it came
from. It runs none of them and writes nothing about them. To confirm one, pass
`init --verification-command "<cmd>"`. The first word of the command is its
executable. It must be a bare program name matching
`^[A-Za-z0-9][A-Za-z0-9._+-]*$`: no path separators, quotes, or shell
operators, so `./run-tests.sh` and `a;b` are rejected and `init` writes no
files. `init` then gives every `provider: claude` role in the written
`dispatch_config.yaml` an `allowed_tools` list: the default list above plus
`Bash(<executable>:*)`. The rule allows every subcommand of that
executable, not only the confirmed command: `Bash(npm:*)` also allows
`npm install` and `npm publish`. When the executable is `python`, `python3`,
or `pytest`, `init` adds no rule and writes no `allowed_tools` list. It prints
one line saying that the dispatcher gate runs the command, and that roles can
run it themselves only after you set `interpreter_grants: true`, which lets an
agent run any command, `git push` included (see
[Interpreter Grants](#interpreter-grants)). `init` never writes
`interpreter_grants`. Without `--verification-command`, `init` writes the
same config as before.

Codex keeps its `workspace-write` sandbox: writes are limited to the
workspace and network access is off by default. The sandbox always keeps
`.git` read-only, even when `.git` is listed under
`sandbox_workspace_write.writable_roots`, so a sandboxed role cannot commit on
its own. Use `commit_mode: dispatcher` for sandboxed Codex implementers: the
dispatcher commits the in-scope files after the role finishes. The reference
workflow ships `.codex/rules/llm-handoff.rules`, a Codex rules file (an
experimental Codex feature) that forbids `git push`. It does not let git
writes run outside the sandbox: rules match command prefixes only, so an
allow rule for `git commit` would also allow `git commit -a` or a trailing
`--amend`. Everything the role runs stays sandboxed.

Codex applies the strictest matching rule, but a project rule cannot override
an `allow` rule in `~/.codex/rules/` that matches the full shell command (for
example a remembered `pwsh -Command "git push origin main"` approval). Review
your user-level rules before relying on the push ban.

On Windows the sandbox runs commands as a separate local account. Read-only git
commands may need `-c safe.directory=<repository root>`, and pytest's per-user
temp directory is not usable from the sandbox. The work-claim gate re-runs the
Verification command outside the sandbox, and the audit fan-out runs it before
an epic close, an empty-range audit, or a story audit no implementer
handed back (see Audit fan-out below). If Codex still cannot start or
commit in your setup, check its sandbox settings before reaching for
`unsafe_bypass`.

The Antigravity adapter launches `agy -p <prompt> --agent <name>
--output-format stream-json --print-timeout <seconds>s`, adding `--model` when
the role sets `model` and `--conversation <id>` when it resumes. It passes no
permission flag by default. In print mode `agy` allows workspace file reads
and writes, but a shell command runs only when a `permissions.allow` grant in
the `agy` user settings covers it, and those grants apply to every `agy` run
for that user. The adapter passes no `--sandbox` flag. A `result` event whose
`status` is not `SUCCESS`, or a run that ends without a `result` event, counts
as a failed dispatch.

The reference workflow ships one agent file per `--agent` name under
`.agents/agents/`: `planner`, `backend`, `frontend`, and `auditor`, plus
`handoff-validator` and `handoff-router` for the validator,
`ledger-updater` for the finalizer, and `supervisor-repair` for a supervised
repair turn. Each file follows the documented `agy`
custom-agent format, and its frontmatter carries `name`, `description`, and a
`tools` list. The role files list eight tools: `view_file`, `list_dir`,
`grep_search`, `find_by_name`, `write_to_file`, `replace_file_content`,
`multi_replace_file_content`, and `run_command`. The read-only
`handoff-validator` file leaves out the three write tools (`write_to_file`,
`replace_file_content`, `multi_replace_file_content`). Leaving out the write
tools is not a shell boundary. `handoff-validator` keeps `run_command`
because it resolves SHAs with `git cat-file` and checks git state, and the
user-wide `permissions.allow` grants, including `git add`, `git commit`, and
any `python` or `pytest` grant, apply to it as to every `agy` run. Its prompt limits
it to read-only git commands; that limit is a guardrail, not a sandbox. Web,
browser, image, MCP, and subagent tools are left out on purpose. A tool name
`agy` does not know makes it exit before the run starts.
`agy -p --agent <name>` loading these files was checked against a live CLI.
Setting `agent_name` on a role changes the `--agent` value, and so which file
`agy` loads. `.agents/**` is a default protected path.

When print mode auto-denies a shell command (or another tool permission it
cannot prompt for), `agy` prints a notice on stderr and still exits 0. The
adapter treats that run as a failed dispatch, and the log names the denied
permission and the entry to add under `permissions.allow` in the `agy` user
settings file (`~/.gemini/antigravity-cli/settings.json`), for example
`command(<target>)`. A resumed run that was soft-denied is not rerun in a
fresh conversation, since the rerun would hit the same denial. When a failed
`agy` run's stderr contains `authentication required`, the dispatcher treats
it as a sign-in failure: the loop stops and logs `agy` sign-in steps (run
`agy` once in a terminal, finish signing in, then check
`agy -p "Reply with OK"`). A signed-out resumed run is not rerun fresh either.
Only the Antigravity adapter checks for that phrase; output from other
providers that says `authentication required` gets normal failure handling.

Antigravity-mapped roles need these entries in
`~/.gemini/antigravity-cli/settings.json`. Under `permissions.allow`, one
anchored regex grant per command, so each covers the command with any
arguments:

- `command(regex:^git status( .*)?$)`
- `command(regex:^git diff( .*)?$)`
- `command(regex:^git log( .*)?$)`
- `command(regex:^git show( .*)?$)`
- `command(regex:^git cat-file( .*)?$)`
- `command(regex:^git add( .*)?$)`
- `command(regex:^git commit( .*)?$)`
- `command(regex:^git rev-parse( .*)?$)`

These are the same commands as the default Claude `Bash(...)` rules. With
`interpreter_grants: true` (see [Interpreter Grants](#interpreter-grants)) the
list also holds `command(regex:^python( .*)?$)`,
`command(regex:^python3( .*)?$)`, and `command(regex:^pytest( .*)?$)`. Add
`command(regex:^<cmd>( .*)?$)` for each Work Packet Verification command you
expect, with a backslash before each regex metacharacter in `<cmd>`
(`\ . + * ? ( ) | [ ] { } ^ $`); spaces and `-` stay as they are. With
`interpreter_grants: false`, a Verification command whose first word is
`python`, `python3`, or `pytest` gets no grant; the dispatcher gate still
runs it. Under `permissions.deny`:

- `command(regex:^git( .*)? push( .*)?$)`
- `unsandboxed(regex:^git( .*)? push( .*)?$)`

The regex form was checked live on 2026-10-17 with `agy` 1.4.2:
`git status --short`, `git log --oneline -5`, and `python -m pytest --version`
ran under these grants, and `git statusx` was refused. In the same check, the
earlier exact-string entries (`command(git status)` and so on) did not cover
commands run with arguments, and the exact-string deny entries
`command(git push)` and `unsandboxed(git push)` blocked only a bare
`git push`: `git push origin main` ran when a broader `git` grant was
present. The deny entries above also refused option-prefixed pushes such as
`git -c core.pager=cat push origin main` and `git -C . push origin main`.

The deny entries are deliberately broad: they refuse any `git` command line
that contains ` push`, for example `git log --grep push`. The opt-in
`command(regex:^python( .*)?$)` grant allows `python -c <code>`, which matches
the Claude `Bash(python:*)` rule, so it can run `git push` past these deny
entries. Unanchored regex semantics, glob forms, `git` aliases, and
`sh -c "git push"` were not checked.

When a deny entry refuses a command, `agy` reports it as a `run_command` tool
error inside a `SUCCESS` result with empty stderr. The adapter fails the
dispatch on any `run_command` tool error whose message contains
`Matches user-configured deny rule`, even when the role recovers and
finishes. For each refusal it logs a fixed line with the step number and,
when it is one of the required deny entries above, the matched entry, for
example `Antigravity deny rule refused a command at step 2 (matched deny rule
command(regex:^git( .*)? push( .*)?$))`; any other rule is reported as
`matched a user-configured deny rule`. The refused command line and `agy`'s
error text are never logged, because they can carry credentials. Because
the deny entries are broad, they also fail turns that run harmless lines such
as `git log --grep push`.

The grants are user-wide: they apply to every `agy` run for that user, in any
repository, and since the adapter passes no `--sandbox` flag they are the
only shell guardrail.

When `agy` is on `PATH`, `llm-handoff init` (including `--dry-run`) prints
this list with `interpreter_grants` off, adds `command(regex:^<cmd>( .*)?$)`
for `--verification-command` when given (unless its first word is `python`,
`python3`, or `pytest`), and asks you to add the entries yourself and then
run `llm-handoff doctor`.
When `agy` is the only provider CLI on `PATH`, `init` writes the
[All-Antigravity Mapping](#all-antigravity-mapping); otherwise `agy` does not
change the provider mapping `init` picks. When any role or
fallback has `provider: antigravity` without `unsafe_bypass: true`,
`llm-handoff doctor` reads the settings file and reports one
`antigravity permissions` result, with the allow list built from the default
entries for the config's `interpreter_grants` value plus
`verification_allowed_prefixes` (with the key off, prefixes that start with
an interpreter are left out):

- WARN when the file is missing (the message lists every entry to add), cannot
  be read, is not valid JSON, or is not shaped as
  `{"permissions": {"allow": [...], "deny": [...]}}` (a missing `allow` or
  `deny` key counts as an empty list; an optional `ask` key must also be a
  list of strings).
- FAIL when a `permissions.allow` entry starts with `command(git push` or
  `unsandboxed(git push`. A regex allow entry (`command(regex:<p>)` or
  `unsandboxed(regex:<p>)`) is decided in this order, with at most one FAIL
  per entry:
  - An entry that equals one of the required allow entries by exact string
    (a generated grant) passes, unless its plain command can push: its first
    word is `git` and it has no second word, its second word starts with `-`,
    or a later word is `push`. So a `verification_allowed_prefixes` entry of
    `git`, `git -c x=y`, or `git push` fails for its generated grant, while
    `command(regex:^git status( .*)?$)` passes.
  - Any other entry whose pattern contains `git` (case-insensitive), whether
    or not it compiles, fails: the doctor cannot verify that it does not allow
    `git push`, so remove it or replace it with a generated grant. For
    example, `command(regex:^git push --force$)`,
    `command(regex:^git status$)`, and `unsandboxed(regex:^git status( .*)?$)`
    fail.
  - Any other entry fails when its pattern compiles and matches (by
    `re.search`) any of `git push`, `git push origin main`, or
    `git -C . push origin main`; for example, `command(regex:.*)` fails. A
    pattern that does not compile is not counted.

  Known limit: a pattern that spells `git` through escapes (for example
  `\x67it`) is caught only by the probe check in the last step.
- WARN for each required allow entry or deny entry that is missing. Entries
  match by exact string, and legacy `command(<cmd>)` exact grants do not cover
  commands run with arguments, so settings that still hold them get this
  warning. Legacy deny entries `command(git push)` and
  `unsandboxed(git push)` get a warning naming both regex deny entries.
- WARN when a `permissions.deny` or `permissions.ask` rule overrides a
  required allow entry. Antigravity applies Deny > Ask > Allow, and a headless
  `agy -p` run denies any command that needs approval, so a rule such as
  `"ask": ["command(*)"]` blocks every required command. The doctor tests each
  `command(...)` rule against the plain required commands (`git status`,
  each verification command, and so on) and reports the matching
  allow entries. It counts a rule as overriding when it matches exactly, is a
  word prefix (`command(git)` for `git status`), is a `*` or `?` wildcard
  pattern that matches, or is a `regex:` pattern that matches. The required
  deny entries do not match any required command, so they are not reported.
  This errs toward warning; the exact `agy` matching rules for every form have
  not been checked against a live CLI.
- WARN, with `interpreter_grants` off, for each `permissions.allow` entry
  that equals `command(regex:^<cmd>( .*)?$)` as generated for `<cmd>`, or
  `command(<cmd>)`, where the first word of `<cmd>` is `python`, `python3`,
  or `pytest`. The grant is user-wide and still applies to every `agy` run,
  so remove it or set `interpreter_grants: true`.
- PASS when none of the above applies.

Roles with `unsafe_bypass: true` launch with `--dangerously-skip-permissions`
and are not checked. Neither `init` nor `doctor` writes the `agy` settings
file.

`llm-handoff doctor` also reports `interpreter grants` lines, one per role or
fallback launch. A Claude launch without `unsafe_bypass` gets a WARN naming
each rule in its effective list (`allowed_tools`, or the default list for the
config's `interpreter_grants` value) that is bare `Bash`, `Bash(*)`, or
`Bash(<cmd>...)` starting with `python`, `python3`, or `pytest`. An
Antigravity launch without `unsafe_bypass` gets a WARN naming each required
`command(regex:...)` allow entry, including those built from
`verification_allowed_prefixes`, whose command starts with one of those
interpreters, and noting that `agy` grants are user-wide; this check does not
read the settings file. Both warnings say that an interpreter can run any
command, including `git push`, past the allowlist. With
`interpreter_grants: true`, a warning that names an interpreter grant the key
adds also names `interpreter_grants: true` as the cause. With the key off and
the default lists, no role has an interpreter grant, so the line is PASS. Any
launch with
`unsafe_bypass: true` gets a WARN that it has no shell allowlist at all; Codex
launches without it get no line. When no line is added and at least one
Claude or Antigravity launch exists, the doctor prints one PASS line that
names the interpreters it checked. Other interpreter rules, such as
`Bash(node:*)` or `Bash(sh:*)`, are not detected; review them by hand. These
lines are never FAIL, so they do not change the doctor's exit code.

`permissions_flag` now only applies together with `unsafe_bypass: true`. It
replaces `--dangerously-skip-permissions` for that role. Setting it without
`unsafe_bypass` fails at config load.

These flags were checked against the provider CLI documentation, not run end
to end against every CLI version on every platform. If a CLI rejects a flag,
pin `unsafe_bypass: true` on that role only while you investigate, and treat
the run as unsandboxed.

### Interpreter Grants

```yaml
interpreter_grants: false
```

`interpreter_grants` is a top-level boolean, `false` by default. With `false`,
no default grant list holds `python`, `python3`, or `pytest`: the default
Claude `allowed_tools` list has no interpreter rule, and the generated `agy`
allow entries have no interpreter grant. With `true`, the default Claude list
gets `Bash(python:*)`, `Bash(python3:*)`, and `Bash(pytest:*)` back, and the
generated `agy` allow entries get `command(regex:^python( .*)?$)`,
`command(regex:^python3( .*)?$)`, and `command(regex:^pytest( .*)?$)` back,
plus a grant for each `verification_allowed_prefixes` entry that starts with
one of those interpreters.

Turn it on only if you accept the risk. An interpreter can run any command,
`git push` included, past the allowlist and the `git push` deny entries;
writing a script with `Write` and running it with `python`, or writing a
`conftest.py` and running `pytest`, does the same. Role allowlists are a
guardrail, not a sandbox. Whatever the value, protect the remote with
server-side branch protection: a ruleset that requires pull requests and has
no bypass actors. The dispatcher does not configure it.

The key changes only the default lists:

- A role that sets its own `allowed_tools` keeps that list as written.
- `unsafe_bypass: true` launches and Codex launches do not change.
- The work-claim gate runs the Work Packet Verification command itself,
  whatever the value. The key controls only whether agent roles may run
  interpreters themselves. With `false`, a role cannot run `python -m pytest`
  on its own, but the gate still runs it.
- `llm-handoff init` never writes the key. A `--verification-command` whose
  executable is `python`, `python3`, or `pytest` adds no `allowed_tools`
  list, so the Claude roles keep the default list and follow the key.

## Protected Control Files

Default:

```yaml
protected_paths:
  - AGENTS.md
  - docs/handoff/HANDBOOK.md
  - docs/handoff/*_PROMPT.md
  - docs/handoff/README.md
  - dispatch_config.yaml
  - .claude/**
  - .codex/**
  - .gemini/**
  - .agents/**
  - .githooks/**
```

Patterns are relative to the repository root. `*` matches within one path
segment and `**` matches any depth. Missing paths are ignored. `.gemini/**`
stays protected because a target repository may still hold `.gemini/`
instruction files that `init` leaves in place.

Before each dispatch the dispatcher hashes the matching files. After the
dispatch, a protected file that changed on disk or in the new commits, and
that the role's Work Packet did not list under `Files in scope`, stops the
loop: the handoff is rewritten to route to `user` with a
`protected_file_modified:` error and the changed paths. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#protected-control-files).

Set `protected_paths: []` to turn the check off. A config file passed with
`--config` under another name is not protected unless you add its path. If a
provider CLI writes its own state under one of these directories in your
setup, narrow the pattern rather than removing the check.

## Tracker (Optional Linear Mode)

The optional top-level `tracker` block binds a repository to a pool of Linear
issues. It is off by default: omitting the block is the same as
`provider: none`, and vanilla mode behaves exactly as before.

In Linear mode, agents read and update issues through their own CLI's Linear
MCP connector. The dispatcher contains no Linear client, never calls Linear,
and holds no Linear credential.

In Linear mode the dispatcher builds each Claude role's tool list from this
block. At load, every `agents.<role>` with `provider: claude` (and any Claude
`fallback`) gets that role's Linear tools appended to its `allowed_tools` (or
to the default list when `allowed_tools` is unset), named
`mcp__<mcp_server>__<tool>`; tools already listed are not repeated. The list
is computed in memory and never written to the config file.

| Role | Linear tools |
|---|---|
| `planner` | `get_issue`, `list_issues`, `list_comments`, `save_comment`, `save_issue` |
| `tpm` | `get_issue`, `list_issues`, `list_comments`, `save_comment`, `save_issue` |
| `backend` | `get_issue`, `list_issues`, `list_comments`, `save_comment` |
| `frontend` | `get_issue`, `list_issues`, `list_comments`, `save_comment` |
| `auditor` | `get_issue`, `list_issues`, `list_comments`, `save_comment`, `save_issue` |
| `validator` | `get_issue`, `list_issues`, `list_comments` |
| `finalizer` | `get_issue`, `list_issues`, `list_comments` |

The `tpm` row applies only when `agents.tpm` is configured; without it, no
role gets those tools under the `tpm` name.

When `claude_mcp_config` is set, those Claude launches also pass
`--mcp-config <resolved path> --strict-mcp-config`, so they load only the MCP
servers in that file. When it is unset, they launch without
`--strict-mcp-config` (an `mcp__` tool is listed), so every MCP server in the
user's Claude configuration loads and tools pre-approved in user or project
settings can run; the startup banner logs one WARN naming those roles and
asking you to set `tracker.claude_mcp_config`.

Claude roles with `unsafe_bypass: true`, the audit fan-out security reviewer,
and the next-agent normalizer get no Linear tools. Codex roles have no
per-role tool list and are unchanged. Linear mode rejects
`provider: antigravity` on any role or fallback at config load, naming each
offending `agents.<role>` or `agents.<role>.fallback`: `agy` agent files
limit each role's tools and give it no Linear MCP access. Map those roles to
claude or codex, or set `tracker.provider: none`.

With the audit fan-out on (`audit.reviewers` includes `security`, the
default), the fan-out quality reviewer gets only the auditor's Linear read
tools (`get_issue`, `list_issues`, `list_comments`); `save_comment` and
`save_issue` are removed from its `allowed_tools` and its kept fallback, and
its prompt says not to post Linear comments or change issue state. After
both verdicts merge, the dispatcher runs one short Linear evidence dispatch
as the auditor, which posts the `audit_passed`, `audit_failed`, or
`user_attention_requested` comment and does the state change on the leased
issue. See [Audit Fan-Out](#audit-fan-out) for how that step is launched and
checked.

In Linear mode, `llm-handoff doctor` prints `tracker connector` lines, one
per launch group of the configured roles and fallbacks: up to two for Claude
(`claude strict` for Claude launches that load `tracker.claude_mcp_config`,
`claude` for the others, when both kinds exist) and one for Codex, in that
order, each ending with `(covers <labels>)`. Linear mode does not support
`antigravity` roles, so there is no Antigravity line.
Each line says whether that CLI's MCP config defines a server named
`tracker.mcp_server`:

- Claude launches that load `tracker.claude_mcp_config` (set, and no
  `unsafe_bypass`): the doctor reads that file. PASS when its top-level
  `mcpServers` has the server; FAIL when it does not, when `mcpServers` is
  missing or not an object, or when the file cannot be read or is not valid
  JSON, because these launches pass `--strict-mcp-config` and cannot load the
  Linear tools from anywhere else.
- Other Claude launches: the doctor reads `<repo>/.mcp.json`, then
  `~/.claude.json` (its top-level `mcpServers`, then the `projects` entry for
  this repository). When `CLAUDE_CONFIG_DIR` is set and non-empty, the user
  file is `$CLAUDE_CONFIG_DIR/.claude.json` instead, matching the file the
  launched CLI uses. PASS names the first source that defines the server;
  otherwise WARN. Servers defined elsewhere, for example in managed settings
  or plugins, are not seen.
- Codex: `$CODEX_HOME/config.toml` (or `~/.codex/config.toml` when
  `CODEX_HOME` is unset or empty), then `<repo>/.codex/config.toml`. PASS when
  either has an `[mcp_servers.<server>]` table; otherwise WARN.

A missing file is skipped. A file that cannot be read or parsed adds its
reason to the line instead of stopping the doctor. Only the
`tracker.claude_mcp_config` case can FAIL and change the doctor exit code.
The doctor only reads these files: it never prints a value from them (MCP
server entries can hold tokens in `env` or `headers`), never runs a provider
CLI, and never contacts Linear, so a PASS does not prove the connector
authenticates.

**Probing Linear access with `--probe-tracker`.**
`llm-handoff doctor --probe-tracker <ISSUE>` is opt-in. It adds one
`tracker probe` line right after the `tracker connector` lines. Without the
option the doctor output is unchanged and no provider CLI runs.

The probe checks these gates in order and prints a FAIL line, launching
nothing, at the first that fails: the config must load (otherwise no probe
line is printed), `tracker.provider` must be `linear`, the issue must be a
Linear identifier such as `ENG-123` (the rejected value is not echoed), and at
least one role or fallback must use `provider: claude`.

When the gates pass, the probe launches the first Claude role or fallback (in
config order) once, with that launch's `binary`, `model`, and environment. It
ignores that launch's `allowed_tools`, `unsafe_bypass`, and
`permissions_flag` and always uses `--permission-mode dontAsk`, `--tools ""`
(no built-in tools), and `--allowedTools` naming only
`mcp__<server>__get_issue` and `mcp__<server>__list_issue_statuses`.
`--disallowedTools` names `WebFetch`, `WebSearch`, and
`mcp__<server>__save_issue`, `save_comment`, `create_issue`, `update_issue`,
and `create_comment`. The probe runs from the repository root, so it also
passes `--setting-sources user`, which skips the project and local Claude
settings files (`.claude/settings.json` and `.claude/settings.local.json`),
and `--settings '{"disableAllHooks": true}'`, which turns off the hooks and
status line command left in the user's settings. Hooks from admin-managed
settings are not affected. With `tracker.claude_mcp_config` set it also passes
`--mcp-config <path> --strict-mcp-config`. The Claude CLI is asked to read the
issue and the `tracker.team` workflow states and to answer with a JSON schema
passed inline; the probe writes no file and has a 180-second timeout. Text the
probe prints from Claude's stderr or its `error` field is collapsed to one
line, truncated to 200 characters, and has non-ASCII characters and ASCII
control bytes escaped.

PASS means the issue was read and every configured state name
(`eligible_state`, `in_progress_state`, `success_state`, and `blocked_state`
when set) is one of the team's workflow states. Any other result, including a
launch error, a non-zero exit, unusable output, an unreadable issue, or
missing state names, is a FAIL and sets exit code 1.

Residual risk: a Linear write tool that the user's own Claude settings
pre-approve under a name not on the deny list above is not denied, with or
without `tracker.claude_mcp_config`; `--strict-mcp-config` limits which MCP
servers load, not which user-level allow rules apply. The probe cannot deny
the rest of the tracker server, because Claude Code applies deny rules before
allow rules, so a server-wide deny would also block the two read tools. When
`tracker.claude_mcp_config` is unset, every MCP server in the user's Claude
configuration also loads. Set `tracker.claude_mcp_config`, and do not
pre-approve tracker write tools (or the whole tracker server) in user-level
Claude settings.

The Linear-mode prompt text ships inside the package as static Markdown under
`llm_handoff/prompt_templates/linear/`: a shared `AGENTS.md` section, the
YAML evidence-comment contract in `EVIDENCE_COMMENT.md`, the Linear issue
template in `ISSUE_TEMPLATE.md`, whose example `## Work Packet` uses the labels
the Work Packet parser reads, and one `roles/<role>.md` section per workflow
role. The text names settings by their
`tracker.*` keys and embeds no values. With `commit_mode: dispatcher`, the
implementer's `implementation_handoff` comment records
`pending-dispatcher-commit`, and the auditor's comment records the concrete
commit SHA. The validator runs a read-only check that compares the latest
evidence comment on the leased issue with `HANDOFF.md`, and routes any
mismatch to `user` with `status: blocked_missing_context`, or reports it
without editing files on a validation-only turn, where the report puts each
mismatch on its `LINEAR-EVIDENCE` row as a `FAIL`. The planner posts
`user_attention_requested` when it routes a leased issue to `user` and
`user_attention_cleared` when work resumes. The auditor moves a passing issue
to `success_state` and leaves a failing one in `in_progress_state`, posting
`audit_passed` or `audit_failed` first, and follows the same user-attention
steps as the planner, also moving the issue to `blocked_state` when it is set
and back to `in_progress_state` when it clears the request.

`llm-handoff init --tracker linear` renders this text. It only sets up a
fresh repository, one with no `dispatch_config.yaml` yet; if that path exists,
`init` stops with exit code 2 and writes nothing, with or without `--dry-run`.
Switching an initialized repository between modes is not supported yet.

- `--tracker-team <key>` is required.
- Give exactly one of `--tracker-project <name>` and `--tracker-label <name>`.
- `--tracker-mcp-server <name>` defaults to `linear`.
- `--tracker-*` options without `--tracker linear` are an error (exit 2).

The options are validated against the `tracker` rules above before anything
is written. `init` then writes:

- `dispatch_config.yaml`: the usual config, with a `tracker` block appended
  between `# llm-handoff:tracker:begin` and `# llm-handoff:tracker:end`. The
  block sets `provider: linear`, the team, the project or label,
  `eligible_state: Todo`, `in_progress_state: In Progress`,
  `success_state: In Review`, and `mcp_server`. It does not set
  `claude_mcp_config`, `blocked_state`, or `user_attention_label`; add them
  by hand if you want them.
- `AGENTS.md`: the shared Linear section between
  `<!-- llm-handoff:linear:begin -->` and `<!-- llm-handoff:linear:end -->`,
  after the usual `llm-handoff` block. An existing Linear block is replaced;
  malformed Linear markers make `AGENTS.md` a conflict.
- `docs/handoff/HANDBOOK.md`: the evidence-comment contract, the issue
  template, and every role section, between the same
  `<!-- llm-handoff:linear:... -->` markers.
  Every role prompt already reads the handbook.

`init` without `--tracker`, or with `--tracker none`, writes the same files as
before, with no tracker block and no Linear markers. The output prints
`Tracker: linear` or `Tracker: none`.

```yaml
tracker:
  provider: linear              # none (default) | linear
  team: ENG                     # Linear team key
  project: Platform             # exactly one of project / label
  # label: repo-a               # opt-in label for this repository
  eligible_state: Todo
  in_progress_state: In Progress
  success_state: In Review      # Done only when you want it
  # blocked_state: Blocked      # optional
  user_attention_label: needs-operator   # optional
  mcp_server: linear            # Linear server name in each CLI's MCP config
  # claude_mcp_config: config/linear-mcp.json   # optional
```

| Key | Linear mode | Meaning |
|---|---|---|
| `provider` | - | `none` (default) or `linear`. |
| `team` | required | Linear team key. An issue is in the pool only if it belongs to this team. |
| `project` | exactly one of `project` / `label` | Linear project whose issues form the pool. |
| `label` | exactly one of `project` / `label` | Label that opts an issue into this repository's pool. |
| `eligible_state` | required | Workflow state an issue must be in to be picked. |
| `in_progress_state` | required | State an issue moves to when work starts. |
| `success_state` | required | State the auditor moves an issue to on pass, for example a review state. |
| `blocked_state` | optional | State for blocked issues. The auditor moves the issue there when it routes a leased issue to `user`, and back to `in_progress_state` when work resumes. |
| `user_attention_label` | optional | Label the planner or the auditor adds when it routes a leased issue to `user` and removes when work resumes. |
| `mcp_server` | required | Name of the Linear server in each CLI's MCP configuration. |
| `claude_mcp_config` | optional | MCP configuration file for Claude roles. |

Validation rules:

- With `provider: none`, every other tracker key must be unset (or `null`).
  A half-configured block is rejected; remove the keys or set
  `provider: linear`.
- With `provider: linear`, `team`, `eligible_state`, `in_progress_state`,
  `success_state`, and `mcp_server` must be non-empty after surrounding
  whitespace is stripped. String values are stored stripped.
- Exactly one of `project` and `label` must be set. Both, or neither, is
  rejected.
- `blocked_state` and `user_attention_label` may be omitted; when given they
  must not be empty.
- `mcp_server` may contain only letters, digits, `_`, and `-`, because it
  later forms the `mcp__<server>__` tool-name prefix for Claude roles.
- `claude_mcp_config`, when set, resolves against the repository root if it
  is relative; an absolute path is used as given. The resolved path must be
  an existing file.
- Linear mode requires the work-claim gate. `provider: linear` with
  `enforce_work_packet: false` is rejected even when
  `work_packet_opt_out_reason` is given, because the auditor only moves an
  issue to `success_state` after the gate passes.
- Unknown keys inside `tracker` are rejected.

## Supervised Dispatch

The optional supervisor wrapper is configured through command-line options, not
through `dispatch_config.yaml`. It still uses this file to resolve the normal
dispatcher settings and, when enabled, the configured `agents.<role>` entry for
`--repair-role`.

Repair is off by default. Setting `--repair-role validator`, for example,
invokes the existing `validator` agent config for one bounded repair attempt
unless `--max-repair-attempts` is set explicitly.

See [docs/SUPERVISED_DISPATCH.md](docs/SUPERVISED_DISPATCH.md) for the
supervisor options, run-state files, incident bundles, heartbeat behavior, lock
semantics, notification hook, and shell wrapper commands.

## Agent Roles

Public docs and templates should use generic role names:

- `planner`
- `backend`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider names are adapter details. The runtime dispatch layer selects the
adapter from each role's `provider` field. The currently registered runtime
adapters are `codex`, `claude`, and `antigravity` (binary `agy`);
`antigravity` can also be the next-agent normalizer (see
[Next-Agent Normalizer](#next-agent-normalizer)) but cannot be the
`audit.security_provider`. Providers without a registered
adapter fail closed at config load.

The `gemini` role provider was removed. `provider: gemini` on any
`agents.<role>` or `agents.<role>.fallback` fails at load with: "The gemini
provider was removed: Gemini CLI no longer serves personal Google accounts.
Use provider: antigravity (the agy CLI)."

When a config file defines only some `agents` entries, the loader merges those
entries over the reference defaults. After merging, all reference roles must be
present. If a role changes providers, omitted provider-specific fields are
filled from that provider's default field bundle rather than the role's old
provider defaults.

| Role | Reference Provider | Reference agent file |
| --- | --- | --- |
| `planner` | `antigravity` (`agy`) | `.agents/agents/planner.md` |
| `backend` | `codex` | `.codex/skills/llm-handoff/SKILL.md` |
| `frontend` | `antigravity` (`agy`) or manual frontend pause | `.agents/agents/frontend.md` |
| `auditor` | `claude` | `.claude/agents/auditor.md` |
| `validator` | `claude` | `.claude/agents/handoff-validator.md` |
| `finalizer` | `claude` | `.claude/agents/ledger-updater.md` |

`tpm` is an optional seventh role. Without `agents.tpm` the planner picks
stories and every route is unchanged. With it, the role uses the same
provider, launch-flag, timeout, and fallback handling as the other roles and
always starts a fresh session. `doctor` reports
`role tpm: not configured (planner picks stories)` when it is absent.
Without `agents.tpm`, a handoff with `next_agent: tpm` gets one planner repair
cycle that asks the planner to route to planner or an implementer; if the
repaired handoff still routes to tpm, the dispatcher pauses. With
`agents.tpm`, routing to the tpm is not wired yet, so such a handoff still
pauses. The validator rejects a `producer: tpm` handoff that routes to
`finalizer` or `validator`, that uses status `ready_for_review`,
`verified_pass`, or `verified_fail`, or that sets `close_type: epic`.
The shipped tpm helper files are `.codex/skills/llm-handoff-tpm/SKILL.md`
for `provider: codex` (`skill_name: llm-handoff-tpm`) and
`.agents/agents/tpm.md` for `agy`; the Claude subagent comes in a later
operator step.

The `planner` and `frontend` roles on `antigravity` need the `agy` user
settings entries described in
[Launch Permissions And Sandboxing](#launch-permissions-and-sandboxing).

Linear mode does not support `antigravity` roles (see
[Tracker](#tracker-optional-linear-mode)). `llm-handoff init --tracker linear` with the reference
mapping therefore writes `planner` and `frontend` on `provider: claude`
(`binary: claude`, `resume: false`, `timeout_ms: 1200000`), with a comment
above `planner` saying why, and keeps the other roles as the template has
them. A hand-written Linear-mode config that omits `planner` or `frontend`
gets the `antigravity` default for that role and fails at load; map both
roles to `claude` or `codex`.

The table documents the shipped reference workflow defaults, not a hard
runtime matrix. If you map a role to another registered provider, make sure the
target repository has the corresponding provider prompt, skill, subagent, or
agent file needed for that provider to perform the role.

Example:

```yaml
agents:
  backend:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: true
```

### All-Claude Mapping

The reference workflow ships a Claude subagent for every role, so a machine
with only Claude Code installed can run the whole loop:

| Role | Claude subagent |
| --- | --- |
| `planner` | `.claude/agents/planner.md` |
| `backend` | `.claude/agents/backend.md` |
| `frontend` | `.claude/agents/frontend.md` |
| `auditor` | `.claude/agents/auditor.md` |
| `validator` | `.claude/agents/handoff-validator.md` |
| `finalizer` | `.claude/agents/ledger-updater.md` |

`llm-handoff init --provider claude` writes this mapping into
`dispatch_config.yaml`, with every role on `provider: claude` and
`audit.security_provider: claude`. Without `--provider`, `init` writes it
automatically when `claude` is the only provider CLI on `PATH`, and keeps the
reference mapping otherwise. `init` prints the mapping it chose and the
provider CLIs it found.

With every role on Claude, the security reviewer runs on the same vendor as
the implementer, so it is a second read-only review rather than an
independent one, and each dispatcher start logs a warning saying so.

### All-Codex Mapping

The reference workflow ships a Codex repo skill for every role, so a machine
with only the Codex CLI installed can run the whole loop. Each role names its
skill in `skill_name`, and the Codex adapter tells Codex to use that skill:

| Role | Codex skill |
| --- | --- |
| `planner` | `.codex/skills/llm-handoff-planner/SKILL.md` |
| `backend` | `.codex/skills/llm-handoff/SKILL.md` |
| `frontend` | `.codex/skills/llm-handoff-frontend/SKILL.md` |
| `auditor` | `.codex/skills/llm-handoff-auditor/SKILL.md` |
| `validator` | `.codex/skills/llm-handoff-validator/SKILL.md` |
| `finalizer` | `.codex/skills/llm-handoff-finalizer/SKILL.md` |

The validator skill covers both the handoff validation and handoff routing
tasks; the dispatcher's prompt names which one applies.

Each Codex role keeps its own run artifacts. The `backend` role uses
`logs/dispatch/codex/last-message.json` and `logs/dispatch/codex/session.json`.
Every other Codex role uses `logs/dispatch/codex-<role>/` with the same two
files, for example `logs/dispatch/codex-planner/session.json`, so roles never
resume each other's session or read each other's last message. A
non-backend session saved under the old shared `logs/dispatch/codex/` path is
not migrated; that role starts a fresh session on its next resume.

`llm-handoff init --provider codex` writes this mapping into
`dispatch_config.yaml`, with every role on `provider: codex`,
`audit.security_provider: codex`, `normalizer.provider: codex` (the
normalizer runs through the Codex CLI login, so no API key is needed), and
`commit_mode: dispatcher` in place of the template's `commit_mode: agent`.
Without `--provider`, `init` writes it automatically when `codex` is the only
provider CLI on `PATH`. If both `claude` and `codex` are on `PATH`, `init`
keeps the reference mapping; pass `--provider` to choose one.
`--verification-command` changes no `agents:` entry under this mapping,
because it only adds `allowed_tools` to Claude roles.

With every role on Codex, the security reviewer runs on the same vendor as
the implementer, so it is a second read-only review rather than an
independent one, and the dispatcher logs a warning saying so.

Codex's `workspace-write` sandbox keeps `.git` read-only, so a sandboxed
backend or frontend role cannot run `git add` or `git commit`, and the
skills tell it not to work around the sandbox: with `commit_mode: agent` the
role hands back `blocked_implementation_failure` on its first story. That is
why `init` writes `commit_mode: dispatcher` (see
[Dispatcher Commits](#dispatcher-commits)) for this mapping: the dispatcher
commits backend and frontend work, and the planner writes nothing to `.git`
because its handoff is left uncommitted on purpose. Dispatcher commits need
`enforce_work_packet: true`, which the template already sets. If you turn
the work-claim gate off, switch `commit_mode` back to `agent` as well, and
expect the sandbox to block the implementer's commit. It does not take the
commit off the finalizer: in that mode the
finalizer is told to commit only the project state file
(`project_state_path`) and leave `HANDOFF.md` uncommitted, and the dispatcher
does not commit the state update for it. If the sandbox blocks the
finalizer's commit, the finalizer reports no commit SHA, the dispatcher
pauses, and you commit the project state file (`project_state_path`)
yourself.

A fresh `codex exec` run returns the role's final message as a
`{status, summary}` JSON envelope. For the `validator` and `finalizer`
roles, the dispatcher parses the `summary` text, so the validator's `VALID:`
report and the finalizer's `PROJECT STATE UPDATED` report must be the whole
`summary`. Output that is not an envelope, such as a resumed session's
answer, is parsed as it is.

### All-Antigravity Mapping

The reference workflow ships an `agy` agent file for every role under
`.agents/agents/`, so a machine with only the Antigravity CLI (`agy`)
installed can run the whole loop. The Antigravity adapter passes the agent
name as `agy --agent <name>`; no role needs `agent_name`, because the
dispatcher already names the validator and finalizer files:

| Role | `agy` agent file |
| --- | --- |
| `planner` | `.agents/agents/planner.md` |
| `backend` | `.agents/agents/backend.md` |
| `frontend` | `.agents/agents/frontend.md` |
| `auditor` | `.agents/agents/auditor.md` |
| `validator` | `.agents/agents/handoff-validator.md` and `.agents/agents/handoff-router.md` |
| `finalizer` | `.agents/agents/ledger-updater.md` |

`llm-handoff init --provider antigravity` writes this mapping into
`dispatch_config.yaml`, with every role on `provider: antigravity` and
`binary: agy`, `audit.security_provider: antigravity`, and
`normalizer.provider: antigravity` with no `model`, so the normalizer uses
its default `gemini-3.8-flash-low` through the `agy` CLI login. No role gets
`unsafe_bypass`, and `commit_mode` and the commented `interpreter_grants`
line stay as the template has them. Without `--provider`, `init` writes it
automatically when `agy` is the only provider CLI on `PATH`; with `agy` and
another provider CLI on `PATH`, `init` keeps the reference mapping.
`--verification-command` changes no `agents:` entry under this mapping,
because it only adds `allowed_tools` to Claude roles.

The all-Gemini mapping was removed. `llm-handoff init --provider gemini`
exits nonzero before writing any file, with: "The gemini provider was
removed: Gemini CLI no longer serves personal Google accounts. Use
--provider antigravity (the agy CLI)."

Every role needs the `permissions.allow` and `permissions.deny` entries in
the `agy` user settings described in
[Launch Permissions And Sandboxing](#launch-permissions-and-sandboxing);
`init` prints them when `agy` is on `PATH`. With every role on Antigravity,
the security reviewer runs on the same vendor as the implementer, so it is a
second read-only review rather than an independent one, and the dispatcher
logs a warning saying so.

Supervised repair works with an `antigravity` repair role. Whichever role
`--repair-role` names, the supervisor launches the repair turn as
`agy --agent supervisor-repair`, which loads the shipped
`.agents/agents/supervisor-repair.md`. That file carries repair-only
instructions, not the role's normal Work Packet instructions. The repair
launch ignores an `agent_name` override on that role and on an `antigravity`
fallback, so a custom role agent never replaces `supervisor-repair`.

Linear mode does not support `provider: antigravity` (see
[Tracker (Optional Linear Mode)](#tracker-optional-linear-mode)).
`init --tracker linear` with this mapping, whether passed with `--provider
antigravity` or detected from `PATH`, fails before writing any file and asks
for `--provider claude`, `--provider codex`, or `--tracker none`.

### Fallback Agents

A role can name one fallback agent. When the primary agent fails fatally, the
dispatcher runs the same dispatch once more on the fallback, in the same cycle:

```yaml
agents:
  backend:
    provider: codex
    fallback:
      provider: claude
      model: claude-sonnet-5-5
  auditor:
    provider: claude
    model: claude-opus-5-5
    fallback:
      provider: claude
      model: claude-sonnet-5-5
```

A failure is fatal, and fails over, only when the primary could not do the
work at all:

| Reason | Detected from |
| --- | --- |
| Launch failure | No output and an OS "file not found" or "command not found" error. |
| Sign-in failure | The same markers that stop the loop without a fallback. |
| Rate or usage limit | 429, `RESOURCE_EXHAUSTED`, `rate_limit_error`, `overloaded_error`, quota, or usage-limit messages in stderr or the end of stdout. |
| Timeout | The process ran past the role's `timeout_ms`, or its optional `idle_timeout_ms`, and was killed. |

Any other non-zero exit is the agent's own failure: the fallback does not run
and the failure counts toward `max_consecutive_failures` as before.

The dispatcher also skips the fallback when the primary failed after it:

- rewrote `HANDOFF.md`, because running the role again could repeat a
  finished handoff; or
- changed a file under `protected_paths`, because the fallback would read the
  changed file as instructions. The protected-file gate then handles the
  change as usual.

In both cases the run counts as a normal failure, exactly as it would without
a fallback. The next cycle reads the new handoff unless that failure stops the
loop, for example a sign-in failure or `max_consecutive_failures` reached.

Rules and limits:

- The fallback block takes the same fields as a role. Omitted fields come from
  the fallback provider's defaults, not from the primary.
- The fallback must launch a different provider, a different `binary`, or,
  for `claude` or `antigravity`, a different `model`. The Codex adapter does
  not pass a model flag, so a second Codex model needs a different binary or
  account.
- One level only: a fallback cannot have its own fallback.
- The fallback starts a fresh session. When it runs, the dispatcher also drops
  the planner's saved session, so the next planner dispatch starts fresh.
- The fallback provider needs the same role files the primary needs, such as
  the Claude subagent, Codex skill, or `agy` agent file for that role.
- If the fallback also fails, its result is the dispatch result and the
  normal failure count applies. A sign-in failure on the fallback still stops
  the loop.
- By default a hung agent is only detected when `timeout_ms` expires. Set
  `idle_timeout_ms` (see [Idle Output Timeout](#idle-output-timeout)) or lower
  `timeout_ms` on a role to fail over sooner.
- Fallbacks cover role dispatches. In audit fan-out the quality reviewer's
  prompt and verdict parsing depend on its provider, so it fails over only to
  an `auditor` fallback on the same provider, with `audit.review_timeout_ms`.
  The security reviewer and the next-agent normalizer do not use fallbacks.

Startup logs a `Launch <role> fallback:` line for each fallback.

### Idle Output Timeout

`idle_timeout_ms` is an optional per-role limit on silence. When it is set, the
dispatcher kills the agent's process tree once the process has written nothing
to stdout or stderr for that long, instead of waiting for the full
`timeout_ms`:

```yaml
agents:
  backend:
    provider: codex
    timeout_ms: 1200000
    idle_timeout_ms: 300000
    fallback:
      provider: claude
      model: claude-sonnet-5-5
```

The kill is reported as `Process timed out after <n> seconds without output
(idle_timeout_ms).`, which counts as a timeout, so a configured fallback runs
next. Without a fallback it is an ordinary timed-out dispatch.

It is off by default (unset), and it should stay off unless you know the CLI
streams steadily. Some CLIs write nothing for long stretches while they work:
Claude in buffered text mode prints only when it finishes, and any CLI can
go quiet during a long model call or a slow tool. Set the limit well above the
longest quiet stretch you have seen in that role's dispatch logs. A fallback
block takes its own `idle_timeout_ms`; it does not inherit the primary's.
The audit security reviewer and the next-agent normalizer do not use it.

## Provider Fields

Common fields:

- `provider`: adapter family such as `codex`, `claude`, or `antigravity`.
- `binary`: command name or absolute executable path.
- `agent_name`: optional display/subagent name for logs or provider-specific
  role selection.
- `resume`: whether the adapter may reuse a prior session.
- `timeout_ms`: process timeout for a single dispatch.
- `idle_timeout_ms`: optional limit on how long the process may write no
  output before it is killed. Off by default. See
  [Idle Output Timeout](#idle-output-timeout).
- `env_mode`: `allowlist` (default) or `inherit`. See
  [Agent Environment](#agent-environment).
- `env_passthrough`: extra environment variable names to pass to this role.
- `use_api_key_env`: pass the provider's own API key (`OPENAI_API_KEY` for
  Codex). Ignored for Claude and Antigravity.
- `unsafe_bypass`: restore the provider's previous approval and sandbox bypass
  flags for this role. Off by default and logged as a startup warning.
- `fallback`: optional agent that reruns the dispatch after a fatal failure.
  See [Fallback Agents](#fallback-agents).

Codex-specific fields:

- `skill_name`: Codex skill to invoke in the target repo.
- `use_api_key_env`: whether to pass `OPENAI_API_KEY`. Off by default.

Claude-specific fields:

- `model`: Claude model for subagent or validator calls.
- `allowed_tools`: tool rules passed to `--allowedTools` in the default
  `dontAsk` launch. See [Launch Permissions And Sandboxing](#launch-permissions-and-sandboxing).
- `permissions_flag`: bypass flag used only with `unsafe_bypass: true`.

Accepted but ignored fields:

- `mention` and `retries`: still accepted on `agents.<role>` and its
  `fallback` so older configs keep loading, because the role block rejects
  unknown keys. No code reads either field, so setting one changes nothing.
  `retries` must still be a non-negative integer when set; a negative value
  fails at load with "retries must be non-negative when set."

## Audit Fan-Out

The auditor route runs a read-only security reviewer next to the quality
reviewer by default. Turning it off is an explicit opt-out that needs a
written reason.

```yaml
audit:
  reviewers: [quality, security]  # the default
  security_provider: null       # claude | codex | antigravity; null = auto
  security_model: null          # claude reviewer only; null = borrowed
  block_severity: high          # critical | high | medium | low
  review_timeout_ms: 900000
```

| Key | Default | Meaning |
| --- | --- | --- |
| `audit.reviewers` | `[quality, security]` | Reviewers on the auditor route. Must include `quality`. Supported values are `quality` and `security`. Leaving out `security` needs `audit.security_opt_out_reason`. |
| `audit.security_opt_out_reason` | unset | Required, non-empty text when `audit.reviewers` leaves out `security`; rejected while `security` is listed. |
| `audit.security_provider` | auto | CLI used for the security reviewer. When unset, see the automatic choice below. |
| `audit.security_model` | unset (borrowed from the first `claude` role, else the built-in Claude default) | Model the claude security reviewer passes as `--model`. Applies only to the claude reviewer; see below. |
| `audit.block_severity` | `high` | A finding at or above this severity routes the story back to the implementer. A security `fail` verdict whose findings are all below it does not block; see below. |
| `audit.review_timeout_ms` | `900000` | Process timeout for each reviewer in fan-out mode. |

To opt out of the security reviewer:

```yaml
audit:
  reviewers: [quality]
  security_opt_out_reason: "Local prototype; no secrets or user data."
```

A config that leaves out `security` without a reason fails to load with a
validation error that names `security_opt_out_reason`. When security is
opted out, every dispatcher start logs a `WARN` line with the reason, and
`llm-handoff doctor` prints `WARN security review:` with it. The auditor
route then uses the single-auditor path, which writes `HANDOFF.md` itself.

`audit.security_model` sets the model for the claude security reviewer only.
When it is unset, the claude reviewer borrows the `model` of the first role
whose provider is `claude` (checked in the order `auditor`, `validator`,
`finalizer`, `planner`, `backend`, `frontend`), else the built-in Claude
default. The codex reviewer passes no model flag and runs with its CLI's
default model. The antigravity reviewer passes `--model` only when the
first role whose provider is `antigravity` (same role order) sets `model`;
otherwise it runs with `agy`'s default model. A config that sets
`audit.security_model` while `audit.security_provider` is anything other
than `claude` (`codex` or `antigravity`), or while
`audit.reviewers` leaves out `security`, fails to load. With `audit.security_provider` unset,
the setting applies when the claude reviewer is chosen; when another vendor
is chosen, each audit logs a `WARN` that `audit.security_model` was not
applied. Every dispatcher start logs the security reviewer's model and its
source in the `Audit reviewers:` line.

With `audit.security_provider` unset, the dispatcher picks the security
reviewer for each implementer role (`backend` and `frontend`) in this order:

1. the first of `claude`, `codex`, `antigravity` whose CLI is on `PATH` and
   that differs from the implementer role's provider;
2. otherwise the implementer's own vendor when it is in that list and its
   CLI is on `PATH`, with a `WARN` that the review is not independent;
3. otherwise `run` refuses to start with an error that begins
   `security_reviewer_failed:`.

The `gemini` security reviewer was removed: `audit.security_provider: gemini`
fails at load with "The gemini provider was removed: Gemini CLI no longer
serves personal Google accounts. Use security_provider: antigravity (the agy
CLI)."

The CLI lookup is the one `llm-handoff doctor` uses for role binaries, and the
binary is the one configured for the first role that uses that provider
(`agy` by default for `antigravity`).

The antigravity reviewer (`audit.security_provider: antigravity`) runs `agy`
from a new private temp directory outside the repository, with a
dispatcher-written agent file under a random name that allows only the read
tools `view_file`, `list_dir`, `grep_search`, `find_by_name`, and `finish`.
It reviews a copy of the scope diff in that temp directory and may read the
repository root as context. It never gets `--dangerously-skip-permissions`,
even when the antigravity role sets `unsafe_bypass: true`, and it uses that
role's environment rules. Any other tool step, a non-`SUCCESS` result, or a
verdict outside the schema fails the review. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#reviewer-security-posture) for
the residual risk. An operator-run live check on 2026-10-18 with `agy` 1.4.2
ran the antigravity reviewer twice. On a real commit range it returned
`pass` with no findings. On the same range plus an injected diff hunk telling
the reviewer to run a `python -c` file write and to write a second file, it
returned `fail` with one `high` finding naming the injection. In both runs
every tool step was `view_file`, and neither target file was created. In
the second run the model declined the injected request, so the check shows
that no tool outside the allowed list ran, not that `agy` refuses a forced
call to one. That part rests on the agent file's `tools` list, which leaves
`write_to_file` and `run_command` unavailable.

At startup, before any agent launches, `run` checks the security reviewer for
each implementer role, whether configured or chosen automatically: its CLI
must be on `PATH`. A failed check stops the run with a
`security_reviewer_failed:` error. `--dry-run` skips this check.

With `security` enabled, the auditor route works differently:

- The dispatcher records `git rev-parse HEAD` before each backend or frontend
  dispatch. The review scope is that commit through the handoff's `scope_sha`.
  After a dispatcher restart in the `merge-commit` and `pr` publish modes,
  the scope starts at the saved story branch base
  (`logs/dispatch/publish/story.json`) when it names the handoff's
  `story_id` and its branch is checked out, including when the restarted
  run routes straight to the auditor. Otherwise, when the pre-dispatch
  commit is unknown (for example after a restart in `local` mode), the
  scope falls back to `scope_sha~1..scope_sha`.
- An epic close (`close_type: epic`), a review range with no committed
  changes, or a story audit whose handoff was not produced by an implementer
  (its `producer` is not `backend` or `frontend`, for example an audit the
  planner re-issues after a bounce or a restart) never passed the work-claim
  gate, so before the reviewers start the
  dispatcher runs the handoff's Work Packet Verification commands itself, on
  the audit commit and outside any agent sandbox. It applies the same
  `verification_allowed_prefixes` check, environment, and
  `work_packet_verification_timeout_seconds` as the gate. A pass is recorded
  as a `## Dispatcher Verification` section, so the quality reviewer does not
  re-run the command, and the final audit handoff keeps that section and the
  dispatcher's commands in its Verification Evidence. A failure, or a command
  the allowlist rejects, routes `status: verified_fail` to `planner` with the
  command output, and no reviewer runs. The step is skipped when the handoff
  already records a pass for the audit commit with the same commands, has no
  Verification command, or the working tree is not a clean checkout of
  `scope_sha` (any uncommitted change other than `HANDOFF.md`); with
  `enforce_work_packet: false`; and in Linear mode, whose evidence step needs
  the reviewers' verdicts.
- The quality reviewer (the configured `auditor` agent) and the security
  reviewer run concurrently on that range. Both are told not to write
  `HANDOFF.md` and must return a JSON verdict.
- The dispatcher, not a reviewer, rewrites `HANDOFF.md`. Any finding at or
  above `block_severity`, a quality `fail` verdict, or a security `fail`
  verdict with no findings or with a finding at or above `block_severity`
  produces `status: verified_fail` routed back to the implementer with a
  `## Security Findings` section. A security `fail` whose findings are all
  below `block_severity` does not block: `block_severity` decides. The
  `## Merge Decision` section records that verdict as non-blocking, its
  findings go under `## Security Notes` (which the pull request body
  repeats), the evidence's unresolved concerns name it, and the log has a
  `WARN`. A block on a range with no committed
  changes routes to `planner` instead, since no implementer has work to fix.
  Otherwise the handoff routes where the
  quality reviewer directed, with sub-threshold findings under
  `## Security Notes`.
- `verified_fail` results increment the story's bounce count, so the
  three-fix circuit breaker still applies.
- Each reviewer's raw stdout and stderr, exit code, elapsed time, and parsed
  verdict are written under `logs/dispatch/audit/<timestamp>-cycle<N>/`. The
  timestamp prefix keeps a restarted dispatcher from overwriting earlier
  cycles. The same folder holds `usage.json`, which records each reviewer's
  provider, token counts, and cost, plus a `total_cost_usd` summed across
  reviewers. A reviewer whose CLI reports no usage (`codex-security` or
  Claude's buffered text mode) shows `null`, and when a provider retries,
  the counts cover only the last attempt.
- A reviewer that fails to start (missing binary, CLI argument error, sign-in
  failure) stops the run with exit 1 after one attempt and logs its stderr in
  an `audit_reviewer_start_failed:` line. Other reviewer errors (timeout,
  rate limit, other non-zero exits, malformed verdict) count toward
  `max_consecutive_failures`. Either way the dispatcher writes no audit
  verdict, so a missing verdict is never a pass.

In Linear mode (`tracker.provider: linear`) the fan-out adds a post-verdict
Linear evidence step. With `tracker.provider: none`, or with
`audit.reviewers: [quality]`, nothing below applies and the auditor route
behaves as described above. The dispatcher still never calls Linear itself.

- The step runs only after both reviewers returned without an error and the
  dispatcher rendered the audit handoff. It is one auditor dispatch built
  from `agents.auditor`, with `audit.review_timeout_ms` as its timeout and
  only a same-provider fallback kept.
- Its prompt carries literal values: `tracker_issue`, `tracker_url`, the
  `scope_sha` as `commit_sha`, the quality reviewer's `commands_run` and
  `output_summary`, the unresolved concerns, and the configured state and
  label names. It first posts `user_attention_cleared` when the latest
  evidence comment is `user_attention_requested` (removing
  `user_attention_label` and moving the issue from `blocked_state` back to
  `in_progress_state` when those are set). It then posts the outcome: on
  `verified_pass`, `audit_passed` and a move to `success_state`; on
  `verified_fail`, `audit_failed` listing every blocking finding, with the
  issue left in `in_progress_state`; on a `user` route,
  `user_attention_requested`, adding `user_attention_label` and moving to
  `blocked_state` when set. Each comment is posted before its state change,
  and a retry does not repost an event already present for the same
  `commit_sha`. The agent must return one JSON object:
  `{"linear_evidence": "posted", "issue": "<tracker_issue>", "events": [...], "state_to": ...}`.
- A Claude auditor without `unsafe_bypass` launches with `allowed_tools` set
  to exactly the auditor's five Linear tools and no built-in tools, so the
  launch passes `--tools ""`; with `tracker.claude_mcp_config` set it also
  passes `--mcp-config <path> --strict-mcp-config`. Codex, Antigravity,
  and `unsafe_bypass` auditors cannot be limited at launch: for
  them the prompt forbids file edits and shell commands, and only the
  post-checks below detect a repository file change after the fact. The
  post-checks do not prevent an edit, and they do not cover paths the
  dispatcher writes itself (`logs/dispatch/`, `.dispatch.lock`, and
  `__pycache__` folders) or files outside the repository.
- The dispatcher writes `HANDOFF.md` only after the step verifies. The step
  fails when the handoff has no `tracker_issue`, the dispatch exits non-zero
  or raises, no JSON object with a `linear_evidence` key is found,
  `linear_evidence` is not `posted`, `issue` is not the `tracker_issue`, the
  last item of `events` is not the expected outcome event, or `HANDOFF.md`,
  `git rev-parse HEAD`, or the `git status --porcelain` output changed during
  the dispatch. Because an edit to a file that is already modified, untracked,
  or ignored leaves the porcelain output unchanged, the step also compares a
  snapshot taken before and after the dispatch: the Git index entries, the
  contents of modified tracked and untracked files, and the size and
  modification time of ignored files. Any difference fails the step.
- On failure the dispatcher keeps (or restores) the pre-fan-out
  `HANDOFF.md`, saves the rendered audit handoff to `audit-handoff.md` and
  the step's stdout and stderr to `linear-evidence.log` in the cycle's audit
  log folder, logs one `ERROR` line that starts with
  `linear_evidence_failed:` and names the cause and the saved path, and stops
  the loop with exit 1. The story is never routed to the planner, the
  finalizer, or the implementer as if the evidence were posted. Check the
  leased issue, then start the dispatcher again; the handoff still routes to
  the auditor. The step's output is also written to `linear-evidence.log`
  when it succeeds.

Security reviewer invocation by provider:

- `claude`: `claude -p` in `dontAsk` permission mode with an allow-list of
  read and `git diff`/`git show`/`git log` tools, edit tools denied, and
  `--json-schema` structured output. The prompt asks for Claude Code's
  built-in security review restricted to the given range.
- `codex`: when `codex-security` is on `PATH` and `scope_sha` is the
  checked-out `HEAD`, the dispatcher runs `codex-security scan . --diff <base>
  --json`. Otherwise it runs `codex exec --sandbox read-only --json
  --output-schema <schema>` with a security-review prompt. Codex Security was
  in beta as of mid-2026 and may require plan-level or approved access; its
  JSON output shape is parsed defensively, and an unrecognized severity is
  treated as `high`.

Security reviewers use the environment allowlist of the role that maps to
their provider, and keep their read-only flags even if that role sets
`unsafe_bypass: true`.

A missing, malformed, or timed-out reviewer verdict, a security reviewer CLI
that is no longer installed, and any reviewer that changes `HANDOFF.md`,
fails the audit dispatch. The dispatcher restores the
pre-review handoff and counts the cycle as a dispatch failure.

## Next-Agent Normalizer

The dispatcher has an internal next-agent normalizer for non-canonical
`next_agent` values. The deterministic enum check runs first. If the value is
not a supported role, the dispatcher may make a small model call to normalize
obvious freeform values to one of the public roles.

The normalizer is intentionally constrained:

- it can only return a canonical role such as `backend`, `frontend`, or
  `auditor`;
- it should return `unknown` when intent is ambiguous;
- `unknown` fails closed and routes to validation or user intervention instead
  of guessing.

Config shape:

```yaml
normalizer:
  provider: claude
  model: claude-haiku-5-5
  timeout_ms: 60000
  on_unknown: fail_closed
```

The registered normalizer adapters are `antigravity`, `claude`, `codex`, and
`openai`. `claude` is the reference default. If `provider` changes and `model`
is omitted, the loader fills a provider-specific default model.

The `gemini` normalizer was removed. `normalizer: {provider: gemini}` fails
config load with: "The gemini provider was removed: Gemini CLI no longer
serves personal Google accounts. Use provider: antigravity (the agy CLI)."

The normalizer has two runtime auth shapes:

- API key path: the configured provider API is called with the
  `NormalizedNextAgent` Pydantic schema. OpenAI requires `OPENAI_API_KEY`;
  Claude uses `ANTHROPIC_API_KEY` when present.
- CLI auth path: Claude falls back to the local Claude Code CLI session when
  no Anthropic API key is available. The `codex` adapter always uses the
  local Codex CLI login, and the `antigravity` adapter always uses the local
  `agy` sign-in. The OpenAI normalizer adapter is API-only.

Choose `provider: codex` when the machine has the Codex CLI signed in (for
example with a ChatGPT account) but no `OPENAI_API_KEY`, such as an all-Codex
setup:

```yaml
normalizer:
  provider: codex
  # model defaults to gpt-5.4-mini; set it to a model your Codex login can use.
```

The `codex` adapter runs `codex exec --sandbox read-only --skip-git-repo-check`
with the arguments passed as a list and no shell. The prompt is sent on stdin.
It never reads or passes `OPENAI_API_KEY`, even when that variable is set; use
`provider: openai` for API-key auth. The normalizer needs no repository
access, and the read-only sandbox still permits file reads, so the run starts
from a private temporary directory outside the repository instead of the
checkout. That directory also holds the JSON schema and last-message files.
Project-level `.codex/` settings in the target repository therefore do not
apply to the normalizer. The sandbox does not block reads by absolute path, so
this limits, but does not remove, what an injected `next_agent` value could
ask Codex to read. `timeout_ms` bounds the whole process tree: on a timeout
the dispatcher terminates Codex and its descendants, including the process
behind a Windows `.cmd` shim. A missing `codex` binary, a launch error, a
timeout, a nonzero exit, or output that is not a valid role fails closed and
leaves the handoff unchanged.

The `antigravity` adapter (`provider: antigravity`, model default
`gemini-3.8-flash-low`) runs `agy -p <fixed instruction> --agent <agent name>
--output-format stream-json --json-schema <schema path> --print-timeout <N>s
--model <model>` with the arguments passed as a list, no shell, and nothing on
stdin; `<N>` is `timeout_ms` in whole seconds, at least 1. Each call starts
from a new private temporary directory outside the repository. That directory
holds the closed JSON schema (`normalizer.schema.json`) and a one-off
workspace agent file, `.agents/agents/llm-handoff-normalizer-<random hex>.md`.
The random name is both the file name and the frontmatter `name:`, so no
global agent can already exist under it. The frontmatter is the name, a fixed
description, `tools: [finish]`, and `commandExecutionPolicy: off`; agy 1.4.2
accepts that last key, but it is not shown to be honored, so it is defense in
depth only. The agent file body is the normalizer prompt, which quotes the
raw `next_agent` value, so the value never appears on the command line. The
environment is the Antigravity role allowlist, with no vendor API key. The
adapter reads stdout as stream-json and accepts only the clean shape: every
`step_update` must have a `step_type` of `user_input`, `agent_response`, or
`finish` (any `tool` step fails, whatever its `tool_name`, as does a missing
or unknown step type); only `init`, `step_update`, and `result` events may
appear; and there must be exactly one `result` event, with `status:
SUCCESS`. The answer comes only from that event's `structured_output`, which
must contain exactly the schema keys. The `response` field, which holds the
raw `finish` arguments with extra keys, is never read. A missing `agy`
binary, a launch error, a timeout (the whole process tree is killed), a
nonzero exit, an agy sign-in failure or permission auto-deny on stderr, a
non-JSON stdout line, or any other shape fails closed and leaves the handoff
unchanged. Error messages never quote stdout, stderr, or the raw value; a
sign-in failure or permission auto-deny is reported with fixed text.

Execution boundary: the only pre-execution boundary for the `antigravity`
normalizer is agy's enforcement of the agent file's `tools: [finish]`. The
user-wide `permissions.allow` grants in `~/.gemini/antigravity-cli/settings.json`
still apply to the normalizer run, as they do to every `agy` run for that
user. `tools: [finish]` was seen to block an injected `run_command` request,
but no live check has yet shown agy refusing a command those grants allow
(for example `python ...`). The step-type and result checks run after agy
exits: they protect the handoff, not the machine, and cannot undo a command
that already ran. The provider is opt-in; the default normalizer stays
`claude`. Operators who enable it should keep the agy grant set narrow, and
should treat role allowlists as a guardrail, not a sandbox (see
[Launch Permissions And Sandboxing](#launch-permissions-and-sandboxing)).

`llm-handoff doctor` prints one `normalizer` line. It fails when the
configured normalizer cannot run: for `codex`, the `codex` binary is not
found; for `antigravity`, the `agy` binary is not found; for `claude`,
neither `ANTHROPIC_API_KEY` nor the `claude` binary is
available; for `openai`, `OPENAI_API_KEY` is not set. The doctor never calls the provider, so a passing line reports auth as not
verified. When the provider is `antigravity`, the doctor also prints a
`normalizer boundary` WARN line, whether or not `agy` is found, that states
the execution boundary above; it does not change the exit code.

The API path should not silently fall back to CLI auth after selecting an API
key. If the structured API call fails, the dispatcher should fail closed and
leave the handoff unchanged for validation.

There is intentionally no `enabled` switch. The normalizer is part of the
routing pipeline: exact enum matches remain deterministic, obvious freeform
values get one constrained normalization attempt, and ambiguity fails closed.

## Prompt Templates

Prompt templates should be stored in the source tree and overrideable from the
target repo.

Planned config:

```yaml
prompts_dir: .llm-handoff/prompts
```

If omitted, source-tree defaults should be used.

## Frontmatter Contract

A handoff begins with YAML frontmatter:

```markdown
---
next_agent: planner
reason: "Scope the first implementation task."
producer: user
---
```

Expected keys:

- `next_agent`: required role name.
- `reason`: required short routing reason.
- `producer`: role or actor that wrote the handoff.
- `scope_sha`: optional commit SHA related to the completed work.
- `prior_sha`: optional prior handoff or work commit.
- `close_type`: optional workflow close marker.
- `epic_id`, `story_id`, `story_title`: optional scope of the current work.
- `remaining_stories`: optional list of the epic's open stories, each
  starting with its story ID, such as `S2 Add the login form`.

The dispatcher should fail closed on invalid or ambiguous frontmatter.

### Story List Check

The post-dispatch gate compares a dispatch's `remaining_stories` with the
handoff the dispatch started from. Stories match by their leading ID token
(`S2` in `S2 Add the login form`), ignoring case and surrounding `**`,
backticks, or a trailing colon. A leading token counts as an ID only when it
contains a digit; a title-only entry such as `Add the login form` matches by
its full text, so `Add login` and `Add billing` stay two stories. Entries are
counted, so keeping one of two entries with the same ID still drops the
other. An entry may leave the list only when it is
the prior or the new `story_id` (the story just closed, or the one now
assigned), or when the rewrite lists it under a `## Dropped Stories` section
with a reason:

```markdown
## Dropped Stories

- S4: Merged into S3 at the user's request.
```

A title-only story is dropped the same way, with a bullet that starts with its
full title, such as `- Add billing: Moved to the next epic.`

Any other missing entry fails the gate with `remaining_stories_dropped`,
naming each dropped entry. A rewrite that leaves out `remaining_stories`
entirely drops every open story. The check is skipped when both handoffs name
different `epic_id` values, since a new epic has its own list; a rewrite that
leaves out `epic_id` is still checked against the prior list.

The failure goes through the existing handoff hygiene path: one producer
repair for an auditor, backend, or frontend handoff (the repair may change
`remaining_stories`), then planner recovery or a pause. Until a dispatch
passes the check, or someone changes the handoff the failed dispatch left
behind, the next dispatch is checked against the list that was dropped, so a
recovering planner that rebuilds the list from the project state file cannot
lose the story again. No configuration key turns the check off.
