# Architecture

`llm-handoff` is built around one operational choice: a Markdown handoff file is
the shared state between agents.

The dispatcher is intentionally serial. One role runs at a time, writes the next
handoff, and exits. The dispatcher validates the result before the next role can
run.

## Design Principles

- Prompts are advisory.
- Validators are authoritative.
- Git is the durable state machine.
- Ambiguous routing fails closed.
- Human-readable state is preferred over hidden orchestration state.

## Core Flow

```text
Read HANDOFF.md
Parse frontmatter
Route next_agent
Invoke provider CLI
Capture output and logs
Validate updated HANDOFF.md
Verify the work claim (opt-in gate)
Continue, pause, or escalate
```

## Module Map

- `llm_handoff.__main__`: CLI entry point.
- `llm_handoff.supervisor`: optional operator wrapper that launches the normal
  dispatcher as a child process, emits heartbeats, writes run state, collects
  incident bundles, and can invoke a configured bounded repair role.
- `llm_handoff.config`: dispatch configuration and repo-root detection.
- `llm_handoff.init_workflow`: target-repo reference workflow initialization.
- `llm_handoff.router`: handoff parsing and route selection.
- `llm_handoff.validator`: post-dispatch handoff validation.
- `llm_handoff.work_claim`: opt-in deterministic work-claim gate (Work Packet
  parsing, `scope_sha` and diff-scope checks, verification command execution,
  bounce handoff).
- `llm_handoff.orchestrator`: dispatch loop and failure-mode handling.
- `llm_handoff.agents`: compatibility facade for older tests and imports; new
  runtime code should use the layered modules directly.
- `llm_handoff.agent_types`: shared dispatch result types.
- `llm_handoff.agent_process`: subprocess execution and path helpers.
- `llm_handoff.agent_streams`: shared CLI stream filtering.
- `llm_handoff.agent_providers.*`: concrete provider CLI adapters.
- `llm_handoff.agent_roles`: generic role-to-adapter invocation wrappers.
- `llm_handoff.handoff_normalizer`: frontmatter `next_agent` normalization
  control flow.
- `llm_handoff.normalizer_models`: normalizer schema and prompt contract.
- `llm_handoff.normalizer_providers.*`: model-provider normalizer adapters.
- `llm_handoff.audit_review`: opt-in audit fan-out with a quality reviewer
  and a security reviewer, structured verdicts, and the merge rule.
- `llm_handoff.ledger`: finalizer-style close flow for durable state updates.
- `llm_handoff.logging_util`: dispatch log writer.
- `llm_handoff.text_io`: robust text decoding for handoff files.

The current source checkout has the core config loader, router, validator,
normalizer, provider invocation layer, target-repo initializer, and prompt
templates. Runtime role dispatch is provider-config driven: each generic role
uses its `agents.<role>.provider` adapter, currently one of `codex`, `gemini`,
or `claude`. The shipped reference workflow still maps planner/frontend to
Gemini, backend to Codex, and auditor/validator/finalizer to Claude, but that
is a template default rather than an execution-layer constraint.

## Handoff File

Default path:

```text
docs/handoff/HANDOFF.md
```

Supported flat path:

```text
HANDOFF.md
```

The handoff file contains two layers:

- YAML frontmatter for machine routing.
- Markdown body for human and agent context.

## Next-Agent Normalizer

The router treats canonical frontmatter as the authority. Supported public
roles are `planner`, `backend`, `frontend`, `auditor`, `validator`,
`finalizer`, and `user`.

If `next_agent` is not an exact enum match, the dispatcher can invoke an
internal next-agent normalizer backed by a configured small model. The current
scaffold supports Claude, Gemini, and OpenAI normalizer adapters. Claude is the
reference default. This is not a workflow role and not a general reasoning
step. It is a constrained resolver for obvious freeform values.

The normalizer has two execution paths. If a provider API key is available, it
uses a structured API call with the Pydantic `NormalizedNextAgent` schema. If
no API key is available, the Claude adapter can fall back to the local Claude
Code CLI session. Gemini and OpenAI normalizer adapters are API-only. Once the
API path is selected, API errors fail closed instead of silently switching to
local OAuth state.

All normalizer provider adapters keep the same schema and failure contract.

Normalizer outcomes:

- canonical role: rewrite `next_agent` and continue through deterministic
  routing;
- `unknown`: fail closed and send the handoff to validation or user
  intervention;
- invalid model output: fail closed rather than inventing a route.

## Git As State

The dispatcher expects Git commit SHAs to appear in handoffs that claim
completed work. When the handoff is inside a Git worktree, `scope_sha`
validation is both syntactic and semantic:

- syntax: 7 to 40 hexadecimal characters;
- semantics: `git cat-file -e <sha>^{commit}` succeeds in the target repo.

Git is not an interchangeable backend in this design. It is part of the
mechanism.

## Failure Modes

The dispatcher should name and isolate failure modes rather than guessing:

- unknown route;
- low-confidence route;
- stale handoff;
- handoff hygiene failure after dispatch;
- planner self-loop;
- agent self-loop;
- missing route after dispatch;
- malformed validator output;
- malformed close/finalizer output;
- audit fan-out reviewer failure: `review_scope_unavailable`,
  `security_extension_missing`, `quality_verdict_malformed`,
  `security_verdict_malformed`, reviewer exit or timeout, or
  `reviewer_wrote_handoff`;
- work-claim gate failure (opt-in): missing or unparseable Work Packet,
  `scope_sha` not at HEAD, scope violation, uncommitted changes, a failed,
  missing, or timed-out configured scanner, or a failed verification command;
- protected control file modified outside the Work Packet scope.

When a failure mode is detected, the safe default is to pause or dispatch a
validator role with a scoped prompt.

For narrow post-dispatch handoff hygiene failures, the dispatcher has a bounded
repair lane. The producer gets one repair-only dispatch constrained to
`docs/handoff/HANDOFF.md`; the repair must create a commit, change the handoff
hash, touch only the handoff file, preserve critical frontmatter, leave dirty
state unchanged except for the handoff, and pass validation. With
`commit_mode: dispatcher` the handoff is never committed: the repair must leave
HEAD where it was (handoff-only commits are undone with `git reset --soft`) and
keep the handoff uncommitted. When the dispatcher committed nothing for a
`ready_for_review` claim and the only validation errors are the missing
`scope_sha` or commit SHA, it skips the repair: a repair could only cite a
commit the implementer did not make, so the work-claim gate reports the claim
instead. If that producer
repair fails, the planner gets one cleanup-only attempt. If the planner cannot
operationalize the handoff, the dispatcher aborts to the user.

## Work-Claim Gate

`validate_handoff` checks the shape of a completion claim: frontmatter fields,
a SHA that exists, a populated evidence block. It does not check that the work
was done. By default that judgment belongs to the auditor, which costs a full
agent session even when the tests already fail.

`enforce_work_packet: true` adds a deterministic gate in
`llm_handoff.work_claim`. It runs right after post-dispatch validation succeeds
for a `status: ready_for_review` handoff from `backend` or `frontend`:

1. Parse the `## Work Packet` from the handoff the implementer received:
   `Files in scope`, `Files out of bounds`, and `Verification command`. A
   missing or unparseable packet is an error (`work_packet_missing`,
   `work_packet_unparseable`), not a skip. A path listed under both fields is
   unparseable: the narrowest entry decides scope, so the tie would make the
   file out of bounds while the packet says to change it.
2. Require `scope_sha` to resolve to the current HEAD of `repo_root`
   (`scope_sha_not_head`). A pre-existing commit pasted as `scope_sha` fails.
   Commits above `scope_sha` that touch only the handoff file are tolerated,
   because some repository protocols commit the handoff after writing the SHA
   into it. With `commit_mode: dispatcher`, an empty `scope_sha` is skipped
   when no commit since dispatch changed a file other than the handoff, because
   the dispatcher had nothing to commit and `no_changes` reports that.
   `no_changes` counts commits since the story's first dispatch, so a retry
   that only reverts a forbidden file passes on the work an earlier dispatch
   of the story committed; the dispatcher records that HEAD as `scope_sha`.
3. Diff the pre-dispatch HEAD against the current HEAD and require every
   changed path to be inside `Files in scope` or to be the handoff file. Any
   changed path under `Files out of bounds` is a `scope_violation`, and so is
   a new uncommitted change to a file outside `Files in scope` or under `Files
   out of bounds`: committing it cannot fix the claim, so the bounce says to
   revert it. Other new uncommitted changes outside the handoff file are
   `uncommitted_changes`, because the verification command runs against the
   working tree and the claim must be reproducible from `scope_sha`. With
   `commit_mode: dispatcher` neither bounce asks the implementer to commit.
4. Run the configured `scanners` (for example gitleaks, semgrep, bandit, and
   pip-audit) in config order, each through the shell in `repo_root` with its
   own `timeout_ms`, with `{diff_range}`, `{changed_files}`,
   `{changed_py_files}`, and `{repo_root}` substituted from the values the
   earlier checks already computed. Scanners run only when steps 2 and 3
   passed, and every scanner runs even after one fails so a single bounce
   carries every finding. A non-zero exit is `scanner_failed:<name>`, a
   timeout is `scanner_timeout:<name>`, and a missing executable is
   `scanner_missing:<name>` for a required scanner or a WARN-level skip for an
   optional one. A command whose placeholders cannot be filled is
   `scanner_config_invalid:<name>`, rejected at config load. The list is
   empty by default.
5. Run the `Verification command` through the shell in `repo_root`, bounded
   by `work_packet_verification_timeout_seconds`. A non-zero exit or a timeout
   is `verification_failed`. The command, exit code, and a bounded output tail
   go to the dispatch log, as do scanner output tails.

Each check has its own error prefix so repair and recovery code can key on it.
On any failure the dispatcher does not dispatch the auditor. It rewrites
`HANDOFF.md` into a bounce: `status: verified_fail`, `producer: validator`, a
quoted `reason` naming the failed checks, a `## Dispatcher Verification`
section with the evidence, the original `## Work Packet` carried forward
verbatim, and the implementer's handback preserved under `## Prior Handback`.
The bounce goes through `record_status_transition`, so the three-fix circuit
breaker counts it. Scope, SHA, worktree, scanner, and verification failures
route back to the same implementer. A missing or unusable Work Packet routes
to `planner`, which owns that artifact. Unreadable Git state and a scanner
command that cannot be rendered escalate to `user`.

Running the Work Packet's verification command is execution of agent-written
text. The dispatcher already launches the implementer with provider
permissions disabled in the same repository, so the planner and implementer
could have run that command themselves; the gate adds no new exposure. It does
mean the dispatcher process, not a provider CLI, runs the command, so keep
`repo_root` pointed at a repository whose handoff files you trust (see
`SECURITY.md`).

Scanner commands sit on the other side of that boundary. They come from
`dispatch_config.yaml`, which the operator writes, so they are trusted
operator-authored input rather than agent-written text. The dispatcher still
does not sandbox them: a scanner runs with the same permissions as the
dispatcher and as the agents it launches, in the same repository, and the only
limits are its `timeout_ms` and the process-tree kill on timeout.
## Protected Control Files

Roles read `AGENTS.md`, the handoff prompts, provider agent files, and the
dispatch config as instructions. A role that edits those files can change what
the next role is told to do, so the dispatcher treats them as protected.

Before every dispatch (role dispatches, finalizer, validator runs, and the
handoff repair lane) the orchestrator hashes every file that matches
`protected_paths` and records `HEAD`. After the dispatch it compares the
hashes and lists the protected files each new commit touched, so a change
that was committed and then reverted, on disk or by a later commit, still
counts. Nested repositories and worktrees inside a protected directory are
skipped. The handoff file itself is never protected.

A change is allowed when the Work Packet the role received lists the path, or
a parent directory ending in `/`, under `Files in scope`. Any other change
fails closed. The dispatcher logs a `protected_file_modified:` error, rewrites
the handoff to `next_agent: user` with `producer: validator`, lists the changed
paths, preserves the agent's handoff text in a fenced block, and skips
post-dispatch validation for that cycle. It does not revert the changes; a human reviews them.

## Dispatch Lock

The CLI creates `.dispatch.lock` in the target repo before starting the loop and
removes it on exit. If the lock already exists, dispatch fails closed with a
message telling the user to confirm no other dispatcher is active before
removing the lock. This protects the handoff file and Git state from two local
dispatcher loops writing the same target repo at the same time.

## Supervised Dispatch

`llm_handoff.supervisor` is an opt-in wrapper around the normal serial
dispatcher. It is intentionally outside the core loop: the child process still
runs `python -m llm_handoff`, owns `.dispatch.lock`, reads `HANDOFF.md`, invokes
one role at a time, and validates the result.

The supervisor adds operator evidence around that child process:

- `logs/dispatch/supervisor/current-run.json` records the latest route, child
  pid, commit SHA, handoff SHA, dirty files, repair role, and status.
- `logs/dispatch/supervisor/supervisor-summary.jsonl` records important events.
- `logs/dispatch/supervisor/dispatch-supervised.lock` prevents two supervisor
  wrappers from managing the same target repo.
- `logs/dispatch/supervisor/incidents/` stores failure bundles with log tail,
  handoff text, Git status, and failure packets.

Heartbeats are emitted every 300 seconds by default and whenever child output
crosses a recognized cycle or route boundary. The heartbeat is only an operator
signal; it is not an alternate state machine.

The optional repair path is disabled unless `--repair-role` is provided. When
enabled, the supervisor writes a failure packet, invokes the configured role
through `llm_handoff.agent_roles.invoke_support_role`, requires a new HEAD
commit, checks that committed and uncommitted changes stay within an allowed repair
surface, and then
restarts the child dispatcher. If the repair is ambiguous, repeats the same
failure fingerprint, fails to commit, or touches files outside scope, the
supervisor stops with an incident bundle.

This wrapper should not be described as broad self-healing. It is a local
operator tool for retries, evidence capture, and optional configured-agent
repair of dispatcher or handoff-protocol failures.

## Provider CLIs

Provider CLIs are adapters, not workflow roles. A public config should map
generic roles such as `planner`, `backend`, or `frontend` to concrete providers
such as Gemini, Codex, or Claude.

`llm_handoff.agent_roles.invoke_role` is the adapter registry boundary. The
orchestrator passes the target role's `AgentConfig`; provider modules translate
that generic role invocation into concrete CLI flags, prompt mentions,
subagent names, session-resume behavior, and timeouts.

The dispatcher does not sandbox these tools itself and does not manage their
auth. It chooses the launch flags: by default each CLI runs under its own
permission or sandbox mode (Claude Code `dontAsk` with an allowlist, Codex
`workspace-write`, Gemini CLI container sandbox), and a role must set
`unsafe_bypass: true` to launch with approvals and sandboxing disabled. It
also builds each launch environment from an allowlist. See
[CONFIGURATION.md](../CONFIGURATION.md#launch-permissions-and-sandboxing).

## Provider-Native Subagents

Provider-native subagents are allowed only inside a single active role
invocation. Claude subagents, Codex skills, and Gemini agents can help the
active role read, reason, review, or execute provider-specific workflows, but
they are not dispatcher roles.

Only the active dispatcher role writes `HANDOFF.md`. Internal provider helpers
must not independently rewrite `HANDOFF.md`, update project-state files, or
create competing routing state. This keeps the handoff file as the mutex and
preserves the serial dispatch contract.

## Audit Fan-Out

By default the auditor route runs one reviewer that both judges the work and
rewrites `HANDOFF.md`. Setting `audit.reviewers: [quality, security]` changes
that route into a read-only fan-out:

1. The dispatcher computes a concrete Git range: the `HEAD` recorded before
   the story's first implementer dispatch, through the handoff's `scope_sha`.
   Retries after a `verified_fail` bounce keep that base, so the range covers
   commits from every attempt; it restarts when the story changes (its
   `story_id`, or its `## Work Packet` when there is no `story_id`) or the
   dispatcher accepts a `verified_pass` handoff. An approval that fails
   validation or triggers recovery keeps the base. The base lives in memory,
   so after a dispatcher restart the next implementer dispatch records a new
   one. Without a recorded base it falls back to `scope_sha~1`. Reviewers are
   never asked to scan the whole repository.
2. The quality reviewer (the configured `auditor` agent) and a security
   reviewer run concurrently in two threads through the existing streaming
   process runner.
3. Each reviewer returns a JSON verdict validated by a Pydantic model:
   `verdict` (`pass` or `fail`) and `findings[]` with `severity`, `file`,
   `line`, `title`, `description`, and `recommendation`. The quality verdict
   also carries the forward route and the evidence fields.
4. The dispatcher merges the verdicts and writes `HANDOFF.md` itself. A
   finding at or above `audit.block_severity`, or either reviewer returning
   `fail`, routes back to the implementer with `status: verified_fail`.
   Otherwise the handoff follows the quality reviewer's route.

The single-writer rule from the provider-native helper boundary still holds:
in fan-out mode the dispatcher is the only writer of `HANDOFF.md`. Reviewers
are told not to edit it, and the dispatcher hashes the file around each
reviewer run. A changed hash is a hard `reviewer_wrote_handoff:` error; the
dispatcher restores the pre-review content and treats the cycle as a failed
dispatch. Any malformed or missing verdict fails closed the same way.

Choosing a security vendor different from the implementer's provider avoids
asking a model family to grade its own output. When `audit.security_provider`
is unset, the dispatcher picks the first of `claude`, `codex`, and `gemini`
that differs from the implementer role's provider.

### Reviewer Security Posture

- The security reviewer gets read-only tool permissions where the CLI supports
  them: Claude runs in `dontAsk` mode with an allow-list of read tools and
  `git diff`/`git show`/`git log`, and edit and web tools denied; Gemini runs
  in `--approval-mode plan` inside its container sandbox; the Codex fallback
  runs `codex exec --sandbox read-only`. The Codex Security CLI has no
  documented write mode for scans. These restrictions apply even when the
  provider's role sets `unsafe_bypass: true`; for Gemini that setting only
  drops the container sandbox. Without Docker or Podman, a Gemini security
  reviewer fails closed with `security_reviewer_failed:`.
- Gemini plan mode alone is not treated as a read-only boundary. Gemini CLI's
  plan mode docs say that in non-interactive runs the policy engine
  auto-approves `exit_plan_mode` and the CLI then switches to YOLO mode. A
  reviewer talked into "executing the plan" by content in the diff would
  then have every tool approved. The container sandbox still holds in that
  case, so the reviewer follows the same rule as Gemini role launches. When
  `audit.security_provider` is `gemini`, `run` checks for a sandbox runtime
  at startup and refuses to start without one, unless the Gemini role whose
  config the reviewer uses sets `unsafe_bypass: true`.
- The quality reviewer keeps the configured auditor permissions because it
  runs verification commands. The handoff hash check, not a sandbox, enforces
  that it does not write `HANDOFF.md`. Other file writes by the quality
  reviewer are not detected by the fan-out.
- Every security reviewer, including both Codex paths, launches with the same
  allowlisted environment as its provider's role (`env_mode`,
  `env_passthrough`, and the vendor API key rule). Claude reviewers run
  without `ANTHROPIC_API_KEY`, and Gemini reviewers run without
  `GOOGLE_API_KEY` and, unless opted in, without `GEMINI_API_KEY`.
- Vendor security reviewers are LLM tools. They are not hardened against
  prompt injection in repository content such as source comments, docs, or
  commit messages. The fan-out reduces that risk by bounding the scope,
  denying writes, requiring a structured verdict, and failing closed on bad
  output. It does not eliminate it. A crafted change can still talk a reviewer
  into a `pass`.

## Circuit Breakers

The dispatcher tracks a three-fix circuit breaker per `story_id`. Each
`verified_fail` handoff increments the story's bounce count. `verified_pass`
and `escalate_to_user` reset it.

When `bounce_count >= 3` and the next route would send the same story back to an
implementer, the dispatcher emits `THREE_FIX_CIRCUIT_BREAKER` and routes to the
planner for re-scoping. If that story has already gone through planner recovery
and still routes back to implementation, the dispatcher emits
`THREE_FIX_CIRCUIT_BREAKER_ESCALATE` and routes to `user`.

Bounce counts are persisted per `story_id` in
`logs/dispatch/bounce-counts.json` under the target repo and reloaded when the
dispatcher starts, so a restart (including the supervised wrapper's restart
after a repair) does not reset the breaker. `verified_pass` and
`escalate_to_user` remove the story from that file. The `bounce_count`
frontmatter field is informational; the JSON file is the count the dispatcher
acts on.

## Prompt And Agent Templates

The written protocol is part of the runtime surface. The source checkout ships:

- `docs/handoff/*.md` for shared handoff prompts and the live handoff template;
- `.codex/skills/llm-handoff/SKILL.md` for the backend role when served by
  Codex;
- `.gemini/agents/*.md` for planner and frontend role templates;
- `.claude/agents/*.md` for auditor, router, validator, and finalizer support.

These files are templates, not hidden state. A consuming repository can copy
them, fork them, or replace them as long as the frontmatter contract remains
valid.

## Non-Goals

- Parallel execution of workflow roles. The audit fan-out runs two
  read-only reviewers inside one auditor dispatch; it does not run roles in
  parallel.
- Worktree orchestration.
- Pull request automation as a core requirement.
- Non-Git state backends.
- Hosted service mode.
- General plugin framework.
