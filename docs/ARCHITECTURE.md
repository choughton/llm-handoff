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
- work-claim gate failure (opt-in): missing or unparseable Work Packet,
  `scope_sha` not at HEAD, scope violation, uncommitted changes, or a failed
  verification command.

When a failure mode is detected, the safe default is to pause or dispatch a
validator role with a scoped prompt.

For narrow post-dispatch handoff hygiene failures, the dispatcher has a bounded
repair lane. The producer gets one repair-only dispatch constrained to
`docs/handoff/HANDOFF.md`; the repair must create a commit, change the handoff
hash, touch only the handoff file, preserve critical frontmatter, leave dirty
state unchanged except for the handoff, and pass validation. If that producer
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
   `work_packet_unparseable`), not a skip.
2. Require `scope_sha` to resolve to the current HEAD of `repo_root`
   (`scope_sha_not_head`). A pre-existing commit pasted as `scope_sha` fails.
   Commits above `scope_sha` that touch only the handoff file are tolerated,
   because some repository protocols commit the handoff after writing the SHA
   into it.
3. Diff the pre-dispatch HEAD against the current HEAD and require every
   changed path to be inside `Files in scope` or to be the handoff file. Any
   changed path under `Files out of bounds` is a `scope_violation`. New
   uncommitted changes outside the handoff file are `uncommitted_changes`,
   because the verification command runs against the working tree and the
   claim must be reproducible from `scope_sha`.
4. Run the `Verification command` through the shell in `repo_root`, bounded
   by `work_packet_verification_timeout_seconds`. A non-zero exit or a timeout
   is `verification_failed`. The command, exit code, and a bounded output tail
   go to the dispatch log.

Each check has its own error prefix so repair and recovery code can key on it.
On any failure the dispatcher does not dispatch the auditor. It rewrites
`HANDOFF.md` into a bounce: `status: verified_fail`, `producer: validator`, a
quoted `reason` naming the failed checks, a `## Dispatcher Verification`
section with the evidence, the original `## Work Packet` carried forward
verbatim, and the implementer's handback preserved under `## Prior Handback`.
The bounce goes through `record_status_transition`, so the three-fix circuit
breaker counts it. Scope, SHA, worktree, and verification failures route back
to the same implementer. A missing or unusable Work Packet routes to `planner`,
which owns that artifact. Unreadable Git state escalates to `user`.

Running the Work Packet's verification command is execution of agent-written
text. The dispatcher already launches the implementer with provider
permissions disabled in the same repository, so the planner and implementer
could have run that command themselves; the gate adds no new exposure. It does
mean the dispatcher process, not a provider CLI, runs the command, so keep
`repo_root` pointed at a repository whose handoff files you trust (see
`SECURITY.md`).

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
commit, checks that changed files are within an allowed repair surface, and then
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

The dispatcher does not sandbox these tools and does not manage their auth.

## Provider-Native Subagents

Provider-native subagents are allowed only inside a single active role
invocation. Claude subagents, Codex skills, and Gemini agents can help the
active role read, reason, review, or execute provider-specific workflows, but
they are not dispatcher roles.

Only the active dispatcher role writes `HANDOFF.md`. Internal provider helpers
must not independently rewrite `HANDOFF.md`, update project-state files, or
create competing routing state. This keeps the handoff file as the mutex and
preserves the serial dispatch contract.

## Circuit Breakers

The dispatcher tracks a three-fix circuit breaker per `story_id`. Each
`verified_fail` handoff increments the story's bounce count. `verified_pass`
and `escalate_to_user` reset it.

When `bounce_count >= 3` and the next route would send the same story back to an
implementer, the dispatcher emits `THREE_FIX_CIRCUIT_BREAKER` and routes to the
planner for re-scoping. If that story has already gone through planner recovery
and still routes back to implementation, the dispatcher emits
`THREE_FIX_CIRCUIT_BREAKER_ESCALATE` and routes to `user`.

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

- Parallel execution.
- Worktree orchestration.
- Pull request automation as a core requirement.
- Non-Git state backends.
- Hosted service mode.
- General plugin framework.
