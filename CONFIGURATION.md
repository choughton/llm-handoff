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
planner_api_key_env: false

agents:
  planner:
    provider: gemini
    binary: gemini
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
provider guardrails, but neither is a hard boundary. Keep push credentials out
of the dispatch environment; see [SECURITY.md](SECURITY.md#threat-model).

## Runtime Controls

The dispatcher exposes role-based runtime toggles for session reuse and planner
API-key environment handling:

```yaml
backend_resume: true
planner_resume: true
planner_api_key_env: false
```

Provider-named runtime keys from the source extraction are not supported in the
public config. Use the role-based keys shown above.

When `planner_api_key_env` is `false`, planner launches strip
`GEMINI_API_KEY`. `GOOGLE_API_KEY` is always stripped for planner launches.
When `planner_api_key_env` is `true`, `GEMINI_API_KEY` is preserved, but
`GOOGLE_API_KEY` is still stripped.

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
with `pause_recovery: "off"`). It accepts five actions:

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
name its pid, so a request accepted for the new lock is kept.

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

Ctrl+C still aborts at once, as before: the dispatcher kills the running
agent's process tree, releases the lock, and exits with code 130. It is not
the graceful path because a Windows console delivers Ctrl+C to every process
attached to it, including the agent CLI and any `git` command in progress.
Edits the interrupted agent left are not recorded for carry-over; review them
with `git status` before restarting.

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
- the publish story state (`publish/story.json`), so a paused publish resumes;
- the carry-over record (`carry-over.json`); see
  [Dispatcher Commits](#dispatcher-commits).

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
| `work_claim_env_passthrough` | `[]` | Extra environment variable names the Verification command and scanners receive. They always get only `PATH`, `HOME`, `USERPROFILE`, `SystemRoot`, `SystemDrive`, `TEMP`, `TMP`, `LANG`, `LC_ALL`, `TERM`, `USERNAME`, `USER`, `LOGNAME`, `COMSPEC`, `PATHEXT`, `WINDIR`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, and `SHELL`, plus these names. Names match case-insensitively. Vendor API keys are rejected at config load; `CLAUDE_*`, `CODEX_*`, and `GEMINI_*` names are never passed. |
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
implementer dispatch and loads it at startup, so a restart keeps the same rule:
only files whose content is unchanged, for the same story key. It drops files
from the record once the dispatcher commits them, deletes the record when
nothing is left or when an implementer dispatch starts for a different story,
and logs an `INFO` line when it restores one. When Git does not ignore that
path, the record stays in memory only and the dispatcher logs a `WARN` line,
because the untracked file would otherwise show up in the next work-claim
gate.

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
  max_review_rounds: 1 # >1 needs Codex to review every push on its own
  codex_bot_login: chatgpt-codex-connector[bot]
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
later pushes, and the dispatcher never asks for a review. So with
`wait_for_reviewers: [codex]` the dispatcher pushes that commit and pauses,
leaving the pull request open for you to review and merge; without it, the
dispatcher merges the pull request. If the retried
`gh pr merge` fails again, the dispatcher pauses the same way and leaves the
pull request open. The next start retries the merge, or you can merge it
yourself.

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

#### Waiting For The Codex Pull Request Review

With `wait_for_reviewers: [codex]`, `pr` mode with `auto_merge: true` waits
for the Codex code review (the ChatGPT Codex connector for GitHub) of the
exact commit it pushed before merging. Set this only when Codex reviews pull
requests in the repository on its own: the dispatcher never asks for a
review. The dispatcher polls the pull request with
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
  validation pause. After the gate and audit pass again, the dispatcher pushes
  to the same pull request and updates its report. It posts each response as
  the reply on that finding's review thread and resolves the thread. A
  finding without a response keeps its thread unresolved, which blocks the
  merge. A response that starts with `Deferred:` (or `**Deferred:**`) marks
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
  deferred findings, the dispatcher does not pause. It rewrites `HANDOFF.md`
  to route to `planner` with each deferred finding, the implementer's
  response, and the current Work Packet. The planner gets one pass per pull
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
  a finding without a response or a person's comment, is still unresolved.

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
  not just newly opened ones: the dispatcher never requests a review, so
  without per-push reviews the wait times out and the loop stops with the
  pull request left open.
- No finished review within `review_timeout_seconds`, a failed review, or a
  `gh` error: the loop stops and leaves the pull request open. It never
  merges without a finished review.

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
which downloads whatever rules the registry serves at run time.

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
- the provider's own configuration variables: `CLAUDE_*` for Claude Code,
  `CODEX_*` for Codex, and `GEMINI_*`, `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`, plus container sandbox
  variables (`DOCKER_HOST`, `DOCKER_CONTEXT`, `CONTAINER_HOST`,
  `SANDBOX_FLAGS`, `SEATBELT_PROFILE`) for Gemini CLI. The Antigravity CLI
  (`agy`) gets no provider variables: it reuses the sign-in stored under the
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
| `GEMINI_API_KEY` | Gemini roles with `use_api_key_env: true` (or the planner flag above) |
| `GOOGLE_API_KEY` | never |

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
| Gemini CLI | `--yolo --sandbox` | `--yolo --sandbox=false` |
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
  - Bash(python:*)
  - Bash(python3:*)
  - Bash(pytest:*)
```

`Agent` is included because the Claude-side prompts delegate to the named
subagent, and `git cat-file` because the shipped `handoff-validator` resolves
SHAs with it. Setting `allowed_tools` on a role replaces the whole list. The
list is a guardrail, not a sandbox: `Bash(python:*)` still lets the agent run
arbitrary Python. `git push` is not on the list. `llm-handoff doctor` reports
these interpreter rules per role as `interpreter grants` warnings.

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
`Bash(<executable>:*)`, unless the default list already has that rule (for
example `python` or `pytest`). The rule allows every subcommand of that
executable, not only the confirmed command: `Bash(npm:*)` also allows
`npm install` and `npm publish`. Without `--verification-command`, `init`
writes the same config as before.

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
temp directory is not usable from the sandbox; the work-claim gate re-runs the
Verification command outside the sandbox. If Codex still cannot start or
commit in your setup, check its sandbox settings before reaching for
`unsafe_bypass`.

Gemini CLI keeps `--yolo` so tool calls do not prompt, but runs inside its
container sandbox. That needs Docker or Podman on `PATH` (macOS can use
`sandbox-exec`; `GEMINI_SANDBOX` selects a runtime). When no runtime is found
the dispatcher refuses to start and names the key to set, for example
`agents.planner.unsafe_bypass: true`. The adapter repeats the same check
before every Gemini launch. Inside the container, the role sees the mounted
project directory; host tools and credentials outside it may be missing.

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
`handoff-validator` and `handoff-router` for the validator and
`ledger-updater` for the finalizer. Each file follows the documented `agy`
custom-agent format, and its frontmatter carries `name`, `description`, and a
`tools` list. The role files list eight tools: `view_file`, `list_dir`,
`grep_search`, `find_by_name`, `write_to_file`, `replace_file_content`,
`multi_replace_file_content`, and `run_command`. The read-only
`handoff-validator` file leaves out the three write tools (`write_to_file`,
`replace_file_content`, `multi_replace_file_content`). Leaving out the write
tools is not a shell boundary. `handoff-validator` keeps `run_command`
because it resolves SHAs with `git cat-file` and checks git state, and the
user-wide `permissions.allow` grants, including `git add`, `git commit`,
`python`, and `pytest`, apply to it as to every `agy` run. Its prompt limits
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
- `command(regex:^python( .*)?$)`
- `command(regex:^python3( .*)?$)`
- `command(regex:^pytest( .*)?$)`

These are the same commands as the default Claude `Bash(...)` rules. Add
`command(regex:^<cmd>( .*)?$)` for each Work Packet Verification command you
expect, with a backslash before each regex metacharacter in `<cmd>`
(`\ . + * ? ( ) | [ ] { } ^ $`); spaces and `-` stay as they are. Under
`permissions.deny`:

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
that contains ` push`, for example `git log --grep push`. The
`command(regex:^python( .*)?$)` grant allows `python -c <code>`, which matches
the Claude `Bash(python:*)` rule. Unanchored regex semantics, glob forms, `git`
aliases, and `sh -c "git push"` were not checked.

When a deny entry refuses a command, `agy` reports it as a `run_command` tool
error inside a `SUCCESS` result with empty stderr. The adapter fails the
dispatch on any `run_command` tool error whose message contains
`Matches user-configured deny rule`, even when the role recovers and
finishes, and logs the refused command line and the error message with
credential-shaped text (URL userinfo, authorization headers, bearer tokens,
secret-named arguments, common token prefixes) replaced by `[REDACTED]`. The
redaction is best effort, so keep secrets out of command lines. Because
the deny entries are broad, they also fail turns that run harmless lines such
as `git log --grep push`.

The grants are user-wide: they apply to every `agy` run for that user, in any
repository, and since the adapter passes no `--sandbox` flag they are the
only shell guardrail.

When `agy` is on `PATH`, `llm-handoff init` (including `--dry-run`) prints
this list, adds `command(regex:^<cmd>( .*)?$)` for `--verification-command`
when given, and asks you to add the entries yourself and then run
`llm-handoff doctor`.
`agy` does not change the provider mapping `init` picks. When any role or
fallback has `provider: antigravity` without `unsafe_bypass: true`,
`llm-handoff doctor` reads the settings file and reports one
`antigravity permissions` result, with the allow list built from the default
entries plus `verification_allowed_prefixes`:

- WARN when the file is missing (the message lists every entry to add), cannot
  be read, is not valid JSON, or is not shaped as
  `{"permissions": {"allow": [...], "deny": [...]}}` (a missing `allow` or
  `deny` key counts as an empty list; an optional `ask` key must also be a
  list of strings).
- FAIL when a `permissions.allow` entry starts with `command(git push` or
  `unsandboxed(git push`, or is a `command(regex:<p>)` or
  `unsandboxed(regex:<p>)` entry whose pattern matches (by `re.search`) any of
  `git push`, `git push origin main`, or `git -C . push origin main`. For
  example, `command(regex:^git( .*)?$)` fails. A pattern that does not compile
  is not counted.
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
  `python`, each verification command, and so on) and reports the matching
  allow entries. It counts a rule as overriding when it matches exactly, is a
  word prefix (`command(git)` for `git status`), is a `*` or `?` wildcard
  pattern that matches, or is a `regex:` pattern that matches. The required
  deny entries do not match any required command, so they are not reported.
  This errs toward warning; the exact `agy` matching rules for every form have
  not been checked against a live CLI.
- PASS when none of the above applies.

Roles with `unsafe_bypass: true` launch with `--dangerously-skip-permissions`
and are not checked. Neither `init` nor `doctor` writes the `agy` settings
file.

`llm-handoff doctor` also reports `interpreter grants` lines, one per role or
fallback launch. A Claude launch without `unsafe_bypass` gets a WARN naming
each rule in its effective list (`allowed_tools`, or the default list above)
that is bare `Bash`, `Bash(*)`, or `Bash(<cmd>...)` starting with `python`,
`python3`, or `pytest`. An Antigravity launch without `unsafe_bypass` gets a
WARN naming each required `command(regex:...)` allow entry, including those
built from `verification_allowed_prefixes`, whose command starts with one of
those interpreters, and noting that `agy` grants are user-wide; this check
does not read the settings file. Both warnings say that an interpreter can run
any command, including `git push`, past the allowlist. Any launch with
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
segment and `**` matches any depth. Missing paths are ignored.

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
| `backend` | `get_issue`, `list_issues`, `list_comments`, `save_comment` |
| `frontend` | `get_issue`, `list_issues`, `list_comments`, `save_comment` |
| `auditor` | `get_issue`, `list_issues`, `list_comments`, `save_comment`, `save_issue` |
| `validator` | `get_issue`, `list_issues`, `list_comments` |
| `finalizer` | `get_issue`, `list_issues`, `list_comments` |

When `claude_mcp_config` is set, those Claude launches also pass
`--mcp-config <resolved path> --strict-mcp-config`, so they load only the MCP
servers in that file. When it is unset, they launch without
`--strict-mcp-config` (an `mcp__` tool is listed), so every MCP server in the
user's Claude configuration loads and tools pre-approved in user or project
settings can run; the startup banner logs one WARN naming those roles and
asking you to set `tracker.claude_mcp_config`.

Claude roles with `unsafe_bypass: true`, the audit fan-out security reviewer,
and the next-agent normalizer get no Linear tools. Codex and Gemini roles
have no per-role tool list and are unchanged. Linear mode rejects
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
per provider CLI the configured roles and fallbacks launch (in the order
claude, codex, gemini), each ending with `(covers <labels>)`.
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
- Gemini: `<repo>/.gemini/settings.json`, then `~/.gemini/settings.json`
  (or `$GEMINI_CLI_HOME/.gemini/settings.json` when `GEMINI_CLI_HOME` is set
  and non-empty). PASS when either has `mcpServers.<server>`; otherwise WARN.

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

Residual risk: when `tracker.claude_mcp_config` is unset, every MCP server in
the user's Claude configuration loads. A Linear write tool that the user's
Claude settings pre-approve under a name not on the deny list above is not
denied. Set `tracker.claude_mcp_config` to limit the probe to that file's
servers.

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
adapters are `codex`, `gemini`, `claude`, and `antigravity` (binary `agy`);
`antigravity` is a runtime adapter only and cannot be a normalizer or
`audit.security_provider`. Providers without a registered
adapter fail closed at config load.

When a config file defines only some `agents` entries, the loader merges those
entries over the reference defaults. After merging, all reference roles must be
present. If a role changes providers, omitted provider-specific fields are
filled from that provider's default field bundle rather than the role's old
provider defaults.

| Role | Reference Provider |
| --- | --- |
| `planner` | `gemini` |
| `backend` | `codex` |
| `frontend` | `gemini` or manual frontend pause |
| `auditor` | `claude` |
| `validator` | `claude` |
| `finalizer` | `claude` |

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

### All-Gemini Mapping

The reference workflow ships a Gemini agent file for every role, so a machine
with only Gemini CLI installed can run the whole loop. Each role's `mention`
names its agent file (`@<role>` selects `.gemini/agents/<role>.md`), and the
Gemini adapter puts that mention at the front of the prompt:

| Role | Gemini agent file |
| --- | --- |
| `planner` | `.gemini/agents/planner.md` |
| `backend` | `.gemini/agents/backend.md` |
| `frontend` | `.gemini/agents/frontend.md` |
| `auditor` | `.gemini/agents/auditor.md` |
| `validator` | `.gemini/agents/validator.md` |
| `finalizer` | `.gemini/agents/finalizer.md` |

The validator file covers both the handoff validation and handoff routing
tasks; the dispatcher's prompt names which one applies.

`llm-handoff init --provider gemini` writes this mapping into
`dispatch_config.yaml`, with every role on `provider: gemini`,
`audit.security_provider: gemini`, and `normalizer.provider: gemini`.
Without `--provider`, `init` writes it automatically when `gemini` is the
only provider CLI on `PATH`. `--verification-command` changes no `agents:`
entry under this mapping, because it only adds `allowed_tools` to Claude
roles.

Every role runs inside Gemini CLI's container sandbox, so the machine needs
Docker or Podman on `PATH` (macOS can use `sandbox-exec`); see
[Launch Permissions And Sandboxing](#launch-permissions-and-sandboxing).
With every role on Gemini, the security reviewer runs on the same vendor as
the implementer, so it is a second read-only review rather than an
independent one, and the dispatcher logs a warning saying so. That reviewer
also needs the Gemini CLI security extension installed and enabled;
`llm-handoff doctor` checks it.

The normalizer runs through the Gemini CLI sign-in, the same login the roles
use, so no API key is needed; see
[Next-Agent Normalizer](#next-agent-normalizer) for the command it runs.
Setting `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in the dispatcher's
environment switches it to the API path. The normalizer runs only when a
handoff's `next_agent` is not a canonical role; when it fails, that handoff
is left unchanged and the loop pauses.

Gemini CLI returns the role's final answer as plain text in its
`stream-json` output, so the validator's `VALID:` report and the finalizer's
`PROJECT STATE UPDATED` report are parsed as written; the agent files ask
for them as plain lines rather than inside a code fence.

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
  for `claude` or `antigravity`, a different `model`. The Codex and Gemini
  adapters do not pass
  a model flag, so a second model on those providers needs a different binary
  or account.
- One level only: a fallback cannot have its own fallback.
- The fallback starts a fresh session. When it runs, the dispatcher also drops
  the planner's saved session, so the next planner dispatch starts fresh.
- The fallback provider needs the same role files the primary needs, such as
  the Claude subagent, Codex skill, or Gemini agent for that role.
- If the fallback also fails, its result is the dispatch result and the
  normal failure count applies. A sign-in failure on the fallback still stops
  the loop.
- By default a hung agent is only detected when `timeout_ms` expires. Set
  `idle_timeout_ms` (see [Idle Output Timeout](#idle-output-timeout)) or lower
  `timeout_ms` on a role to fail over sooner. Gemini retries its own 429s first (`retries`, with
  backoff); set `retries: 0` on a Gemini role to fail over at the first 429.
- Fallbacks cover role dispatches. In audit fan-out the quality reviewer's
  prompt and verdict parsing depend on its provider, so it fails over only to
  an `auditor` fallback on the same provider, with `audit.review_timeout_ms`.
  The security reviewer and the next-agent normalizer do not use fallbacks.

Startup logs a `Launch <role> fallback:` line for each fallback, and a Gemini
fallback needs a sandbox runtime like any Gemini role.

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
Gemini in plain-text mode (the fallback when `stream-json` is not supported)
and Claude in buffered text mode print only when they finish, and any CLI can
go quiet during a long model call or a slow tool. Set the limit well above the
longest quiet stretch you have seen in that role's dispatch logs. A fallback
block takes its own `idle_timeout_ms`; it does not inherit the primary's.
The audit security reviewer and the next-agent normalizer do not use it.

## Provider Fields

Common fields:

- `provider`: adapter family such as `codex`, `gemini`, or `claude`.
- `binary`: command name or absolute executable path.
- `agent_name`: optional display/subagent name for logs or provider-specific
  role selection.
- `resume`: whether the adapter may reuse a prior session.
- `timeout_ms`: process timeout for a single dispatch.
- `idle_timeout_ms`: optional limit on how long the process may write no
  output before it is killed. Off by default. See
  [Idle Output Timeout](#idle-output-timeout).
- `retries`: provider retry count where supported.
- `env_mode`: `allowlist` (default) or `inherit`. See
  [Agent Environment](#agent-environment).
- `env_passthrough`: extra environment variable names to pass to this role.
- `use_api_key_env`: pass the provider's own API key (`OPENAI_API_KEY` for
  Codex, `GEMINI_API_KEY` for Gemini). Ignored for Claude.
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

Gemini-specific fields:

- `mention`: optional agent mention used by Gemini agent files.
- `use_api_key_env`: whether to preserve `GEMINI_API_KEY` for a Gemini-backed
  role. `GOOGLE_API_KEY` is always stripped by the current adapter.

## Audit Fan-Out

The auditor route runs a read-only security reviewer next to the quality
reviewer by default. Turning it off is an explicit opt-out that needs a
written reason.

```yaml
audit:
  reviewers: [quality, security]  # the default
  security_provider: null       # claude | gemini | codex; null = auto
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
| `audit.block_severity` | `high` | A finding at or above this severity routes the story back to the implementer. |
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
default. The gemini and codex reviewers pass no model flag and run with their
CLI's default model. A config that sets `audit.security_model` while
`audit.security_provider` is `gemini` or `codex`, or while `audit.reviewers`
leaves out `security`, fails to load. With `audit.security_provider` unset,
the setting applies when the claude reviewer is chosen; when another vendor
is chosen, each audit logs a `WARN` that `audit.security_model` was not
applied. Every dispatcher start logs the security reviewer's model and its
source in the `Audit reviewers:` line.

With `audit.security_provider` unset, the dispatcher picks the security
reviewer for each implementer role (`backend` and `frontend`) in this order:

1. the first of `claude`, `codex`, `gemini` whose CLI is on `PATH` and that
   differs from the implementer role's provider;
2. otherwise the implementer's own vendor when its CLI is on `PATH`, with a
   `WARN` that the review is not independent;
3. otherwise `run` refuses to start with an error that begins
   `security_reviewer_failed:`.

The CLI lookup is the one `llm-handoff doctor` uses for role binaries, and the
binary is the one configured for the first role that uses that provider.

At startup, before any agent launches, `run` checks the security reviewer for
each implementer role, whether configured or chosen automatically: its CLI
must be on `PATH`, and a Gemini reviewer also needs a sandbox runtime (unless
the Gemini role sets `unsafe_bypass: true`) and the Gemini CLI security
extension. A failed check stops the run with a `security_reviewer_failed:`
error. `--dry-run` skips this check. `llm-handoff doctor` runs the same
extension check (`gemini extensions list`, no model call) when a reviewer
resolves to Gemini and prints a `security extension` line that fails when
the extension is missing or disabled.

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
- The quality reviewer (the configured `auditor` agent) and the security
  reviewer run concurrently on that range. Both are told not to write
  `HANDOFF.md` and must return a JSON verdict.
- The dispatcher, not a reviewer, rewrites `HANDOFF.md`. Any finding at or
  above `block_severity`, or a `fail` verdict from either reviewer, produces
  `status: verified_fail` routed back to the implementer with a
  `## Security Findings` section. Otherwise the handoff routes where the
  quality reviewer directed, with sub-threshold findings under
  `## Security Notes`.
- `verified_fail` results increment the story's bounce count, so the
  three-fix circuit breaker still applies.
- Each reviewer's raw stdout and stderr, exit code, elapsed time, and parsed
  verdict are written under `logs/dispatch/audit/<timestamp>-cycle<N>/`. The
  timestamp prefix keeps a restarted dispatcher from overwriting earlier
  cycles. The same folder holds `usage.json`, which records each reviewer's
  provider, token counts, and cost, plus a `total_cost_usd` summed across
  reviewers. A reviewer whose CLI reports no usage (Gemini, `codex-security`,
  or Claude's buffered text mode) shows `null`, and when a provider retries,
  the counts cover only the last attempt.
- A reviewer that fails to start (missing binary, CLI argument error, sign-in
  failure, missing Gemini sandbox or security extension) stops the run with
  exit 1 after one attempt and logs its stderr in an
  `audit_reviewer_start_failed:` line. Other reviewer errors (timeout, rate
  limit, other non-zero exits, malformed verdict, a Gemini extension check
  that itself times out or hits a rate limit) count toward
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
  passes `--mcp-config <path> --strict-mcp-config`. Codex, Gemini,
  Antigravity, and `unsafe_bypass` auditors cannot be limited at launch: for
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
- `gemini`: `gemini -p "/security:analyze ..."` in `--approval-mode plan`
  with `--sandbox`, so it needs Docker or Podman like the Gemini roles. Plan
  mode is not enough by itself: headless Gemini CLI auto-approves
  `exit_plan_mode` and then continues in YOLO mode. When Gemini is the
  security reviewer, `run` refuses to start when no sandbox runtime is found,
  instead of failing at the first audit.
  The target repository's Gemini CLI must have the security extension
  installed:

  ```bash
  gemini extensions install https://github.com/gemini-cli-extensions/security
  ```

  The dispatcher checks `gemini extensions list` at startup and again before
  each review, and fails closed with `security_extension_missing:` when the
  extension is absent or listed as disabled for the repository (a cross
  marker, or `Enabled (Workspace): false`). Enable a disabled extension with
  `gemini extensions enable gemini-cli-security`. The extension
  documents its analyze command as interactive-first, so headless results
  depend on the installed extension version.
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

The registered normalizer adapters are `claude`, `codex`, `gemini`, and
`openai`. `claude` is the reference default. If `provider` changes and `model`
is omitted, the loader fills a provider-specific default model.

The normalizer has two runtime auth shapes:

- API key path: the configured provider API is called with the
  `NormalizedNextAgent` Pydantic schema. Gemini uses `GEMINI_API_KEY` or
  `GOOGLE_API_KEY` when present; OpenAI requires `OPENAI_API_KEY`; Claude uses
  `ANTHROPIC_API_KEY` when present.
- CLI auth path: Claude falls back to the local Claude Code CLI session when
  no Anthropic API key is available, and Gemini falls back to the local Gemini
  CLI sign-in when neither Gemini key is set. The `codex` adapter always uses
  the local Codex CLI login. The OpenAI normalizer adapter is API-only.

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

The `gemini` adapter's CLI path runs `gemini --approval-mode plan --policy
<deny-all policy> --sandbox=false --skip-trust --model <model> --output-format
json -p <fixed instruction>` with the arguments passed as a list and no shell,
and the request on stdin (headless Gemini CLI appends the `-p` text to
stdin). Like the `codex` adapter it starts from a private temporary directory
outside the repository, so project-level `.gemini/` settings do not apply and
the call has nothing to read. Plan mode alone is not a tool boundary, because
headless Gemini CLI auto-approves `exit_plan_mode` and then runs in YOLO mode.
The temporary directory therefore also holds a policy file with one rule,
`toolName = "*"`, `decision = "deny"`, `priority = 999`, which `--policy`
loads in the user policy tier. It outranks the built-in plan and YOLO allow
rules and the allow rules derived from Gemini CLI settings, so every tool
call, including `exit_plan_mode` and MCP tools, is denied; only an admin or
system policy on the machine ranks higher. With no tool able to run, no
container sandbox is used, so this path does not need Docker. The environment
is the Gemini role allowlist with both Gemini API keys withheld. The answer is
parsed from the JSON `response` field (a JSON object, a bare role name, or either inside
a code fence); anything else, a nonzero exit, a timeout, or a sign-in failure
(for example `IneligibleTierError`, which Google now reports for the retired
personal Gemini Code Assist tier) fails closed with the sign-in fix in the
message. Setting `GEMINI_API_KEY` selects the API path instead.

`llm-handoff doctor` prints one `normalizer` line. It fails when the
configured normalizer cannot run: for `codex`, the `codex` binary is not
found; for `claude`, neither `ANTHROPIC_API_KEY` nor the `claude` binary is
available; for `openai`, `OPENAI_API_KEY` is not set; for `gemini`, none of
`GEMINI_API_KEY`, `GOOGLE_API_KEY`, or the `gemini` binary is available. The
doctor never calls the provider, so a passing line reports auth as not
verified.

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
