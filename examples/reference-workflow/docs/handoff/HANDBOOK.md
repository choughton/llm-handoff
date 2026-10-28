# Handoff Handbook

Read this before acting on a `docs/handoff/HANDOFF.md` assignment. Role prompts
add role-specific instructions; this handbook contains the shared protocol.

## How HANDOFF.md Works

`HANDOFF.md` is the live state file. Only the active dispatcher role owns the
file during its turn. Provider-native subagents, skills, or helper agents are
internal support machinery and must not independently rewrite the handoff.

Every handoff has two layers:

- YAML frontmatter for machine routing.
- Markdown body for human-readable context, evidence, findings, and work
  packets.

The `tpm` role exists only when `agents.tpm` is configured in the dispatch
config. Then the planner opens each epic and routes to `tpm`, and the tpm
grooms the stories inside that open epic and assigns each story with a Work
Packet. Epic-level direction stays with the planner: it chooses the epic,
defines its intro, goal, boundaries, `Fit:` line, and story outline, and
owns the epic's scope, but does not size or split the stories. Approving the
epic close stays with the auditor, which routes an approved final story to
`finalizer` in both configurations. Without it, the planner grooms and assigns every story itself and no handoff
routes to `tpm`.

## Frontmatter Schema Reference

Every handoff write starts with YAML frontmatter containing `next_agent`,
`reason`, and `producer`. Use the optional scope fields when known:
`epic_id`, `story_id`, `story_title`, `remaining_stories`, `scope_sha`,
`close_type`, `prior_sha`, `status`, `bounce_count`, and `evidence_present`.

Quote every `reason`. Run `git rev-parse HEAD` for concrete SHAs. Never write
`scope_sha: HEAD`.

## Story List

`remaining_stories` is the epic's live story list. Every role that rewrites
`docs/handoff/HANDOFF.md` copies it forward. Remove an entry only when it is
the story just closed or the story the planner or tpm assigns as `story_id`.
To drop any other story, add a `## Dropped Stories` section with one bullet
per story, starting with its ID:

```markdown
## Dropped Stories

- S4: Merged into S3 at the user's request.
```

Inside the open epic, a tpm may also remove a story it split or a story
already done on the default branch. It lists each such removal under
`## Dropped Stories` with its reason, such as
`- EPIC-3: Split into EPIC-7, EPIC-8.` or
`- EPIC-4: Already done on <branch> at <sha>.` Split replacements take new flat
IDs after the epic's highest story number and carry `(was EPIC-N)`. The tpm
records new and groomed stories in `PROJECT_STATE.md` through the Work Packet
it writes. A tpm never drops a story for a scope reason: scope changes, epic
boundaries, and wrong or competing scope decisions route to `planner`.

The dispatcher matches stories by their leading ID token and fails the
post-dispatch gate when a rewrite for the same epic drops a story without such
an entry. A new story belongs in `PROJECT_STATE.md` too; the dispatcher warns
when `remaining_stories` names a story ID the project state file does not
list.

## Status Enum Reference

Use exactly one of these values when status is needed:

- `ready_for_review`
- `verified_pass`
- `verified_fail`
- `blocked_missing_context`
- `blocked_implementation_failure`
- `escalate_to_user`

Do not invent synonyms or change capitalization.

## Transition Table

This table is the producer-to-route-and-status contract that the
llm-handoff validator (`llm_handoff/validator.py`) enforces. The llm-handoff
test suite (`tests/test_transition_table.py`) parses the table and checks
every row against the validator, so a table edit without the matching
validator edit, or the reverse, fails those tests. The validator never reads
this file at runtime.

Each row lists what one `producer` value permits on its own: the `next_agent`
routes, the `status` values, and the `close_type` values it may write.
`(omitted)` means the field is left out. Provenance `agent` covers every
handoff an agent writes. Provenance `dispatcher_authored` applies only when
the caller running the validator built the handoff itself and passes
`dispatcher_authored=True`; it is never inferred from handoff content, and for
every producer other than `validator` it permits nothing beyond the `agent`
row.

| Producer | Provenance | next_agent | status | close_type |
| --- | --- | --- | --- | --- |
| `planner` | agent | `planner`, `tpm`, `backend`, `frontend`, `auditor`, `validator`, `user` | `(omitted)`, `ready_for_review`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story`, `epic` |
| `tpm` | agent | `planner`, `backend`, `frontend`, `auditor`, `user` | `(omitted)`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story` |
| `backend` | agent | `planner`, `auditor`, `user` | `(omitted)`, `ready_for_review`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story` |
| `frontend` | agent | `planner`, `auditor`, `user` | `(omitted)`, `ready_for_review`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story` |
| `auditor` | agent | `planner`, `tpm`, `backend`, `frontend`, `validator`, `finalizer`, `user` | `(omitted)`, `verified_pass`, `verified_fail`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story`, `epic` |
| `finalizer` | agent | `planner`, `tpm`, `user` | `(omitted)`, `verified_pass`, `blocked_missing_context`, `escalate_to_user` | `(omitted)` |
| `validator` | agent | `planner`, `tpm`, `backend`, `frontend`, `auditor`, `validator`, `user` | `(omitted)`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story` |
| `validator` | dispatcher_authored | `planner`, `tpm`, `backend`, `frontend`, `auditor`, `validator`, `user` | `(omitted)`, `verified_pass`, `verified_fail`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story` |
| `user` | agent | `planner`, `tpm`, `backend`, `frontend`, `auditor`, `validator`, `user` | `(omitted)`, `ready_for_review`, `blocked_missing_context`, `blocked_implementation_failure`, `escalate_to_user` | `(omitted)`, `story`, `epic` |

A row permits each value separately. A permitted route and a permitted status
do not make their combination legal; every handoff must also pass these
checks:

- **Status route.** A status names its route, and `next_agent` must agree
  (`frontmatter_status_route_conflict`). `ready_for_review` goes to
  `auditor`. `verified_pass` goes to `finalizer` with `close_type: epic`, to
  `tpm` when `next_agent` is `tpm`, and otherwise to `planner`.
  `verified_fail` goes to the named `backend` or `frontend`, otherwise to
  `planner`. `blocked_missing_context` and `escalate_to_user` go to `user`,
  and `blocked_implementation_failure` goes to `planner`.
- **Epic close.** `close_type: epic` requires `next_agent` `auditor` or
  `finalizer`, and `next_agent: finalizer` requires both `close_type: epic`
  and `producer: auditor` (`frontmatter_routing_rule`). A planner or user
  epic close therefore routes only to `auditor`.
- **Evidence.** `ready_for_review`, `verified_pass`, and `verified_fail` need
  a populated `## Verification Evidence` block naming a commit. The only
  exemption is a finalizer `verified_pass` when both the dispatched role and
  the producer are `finalizer`.
- **SHA.** A set `close_type` needs a `scope_sha`; `scope_sha` and
  `prior_sha` are 7-40 hex characters that resolve to commits; and an
  implementer `ready_for_review` handback names its `scope_sha`.
- **Configured tpm.** `next_agent: tpm` is dispatchable only when
  `agents.tpm` is configured. Otherwise the dispatcher sends one repair to
  the planner, then pauses.
- **Dispatch identity.** After a dispatch, `producer` must name the
  dispatched role (`frontmatter_producer_mismatch`), and the handoff may not
  route back to that role (`planner_self_loop`, `agent_self_loop`). A
  finalizer `verified_pass` after any other role's dispatch is rejected, and
  a tpm dispatch keeps its `epic_id`. Two callers differ. A
  dispatcher-authored handoff, which always names `producer: validator`,
  skips only the identity check. An operator-directed chat takeover rewrite
  may name `producer: planner` or `producer: user`, and no other producer.
  Neither waives any other check.

## Evidence Block Reference

When `status` is `ready_for_review`, `verified_pass`, or `verified_fail`, include
`## Verification Evidence` with these five fields:

- `Commands run`
- `Output summary`
- `Commit SHA verified`
- `Files changed or reviewed`
- `Unresolved concerns`

The finalizer's `verified_pass` epic-close handoff is exempt: it carries the
auditor's approval forward and verifies no commit itself.

Evidence must come from the current turn. Prior output, assumptions, and model
confidence are not evidence.

### Gate-Verified Handback

With `enforce_work_packet: true`, the work-claim gate runs the Work Packet's
`Verification command` itself on the claimed commit before the auditor is
dispatched. The gate is on unless the dispatch config sets
`enforce_work_packet: false`, and dispatcher commit mode always runs it.

An implementer whose run of the Verification command was stopped in the
current turn by an environment refusal, such as a permission refusal or a
missing-tool error, may then still hand back `status: ready_for_review`. Its
`## Verification Evidence` block keeps all five fields and changes two:

- `Commands run` reads `deferred to the work-claim gate: <the refusal, quoted>`,
  quoting the refusal text the shell or tool returned.
- `Output summary` identifies the refusal: the refused command and whether a
  permission rule or a missing tool stopped it.

`Commit SHA verified`, `Files changed or reviewed`, and `Unresolved concerns`
are filled as in any handback, and the `scope_sha` and dispatcher commit mode
conventions (an empty `scope_sha` and `pending-dispatcher-commit`) are
unchanged. Run every command the environment allows and report it as usual;
defer only the refused command. This wording is the one exception to the
evidence schema in `docs/handoff/README.md`, which otherwise asks for verbatim
command lines and exit codes: the deferred entry has neither, and it is still
valid.

Without the gate (`enforce_work_packet: false`), nothing else runs the
command, so the current rule stands: run it and report the result, or hand
back a blocked status. A failing test is not an environment refusal and is
not a passing result. A refusal from an earlier session does not qualify.

The auditor treats the deferred `Commands run` together with a
`## Dispatcher Verification` section whose `Verdict` is `PASS` and whose
`Verified commit` is the claimed commit as valid evidence. When the
implementer committed `HANDOFF.md` after the claimed commit, the section may
name that later commit instead; it still counts when every commit between it
and the claimed commit changes only `HANDOFF.md`. It is not a
finding and not a reason to run the same Verification command again. Only the
dispatcher writes that section, after the gate passes; an implementer never
writes or edits it. A missing section, a `FAIL` verdict, or a section for a
different commit does not establish a pass, so the claim stays unverified.

## Pull Request Review Responses

When the dispatcher bounces pull request review findings back to you, add a
`## Review Responses` section to the handback with one `### Finding N`
subsection per finding: one or two sentences on what you changed, or why you
left it. The dispatcher posts each response as the reply on that finding's
review thread and resolves the thread.

If a finding is real but you leave it unfixed for later work, for example
because the fix is outside the Work Packet's Files in scope, start its
response with `Deferred:`, for example
`Deferred: out of scope for this story; needs its own Work Packet.` The
dispatcher posts that reply but leaves the thread unresolved, so the pull
request is not merged yet. Do not use `Deferred:` for a finding you rejected
as wrong; explain why it does not hold up instead. Deferring every finding is
still a `ready_for_review` handback, with no code change: do not hand back a
`blocked_*` status to defer a finding, because the dispatcher then posts no
reply and the planner never decides it.

When deferred threads are all that keep a pull request from merging, the
dispatcher routes the handoff to the planner once per pull request. The
planner decides each deferred finding:

- Widen the scope: rewrite the Work Packet for the implementer with the same
  `story_id`, add the files the fix needs to Files in scope, and copy the
  findings to fix with their `### Finding N` headings. The implementer
  answers them under `## Review Responses` as above.
- Accept it as follow-up work: plan a follow-up story, then add a
  `## Deferred Finding Decisions` section with a `### Finding N` subsection
  that starts with `Follow-up:` and names that story. The dispatcher posts it
  as the reply, resolves the thread, and merges once every thread is
  resolved.
- Ask the user: route to `user` with one specific question.

A finding the planner neither accepts nor sends back, or one deferred again
after the planner's pass, pauses the dispatcher for a person. Decisions written
outside that pass are acted on only when they decide every open finding and
the pull request head is the reviewed commit; otherwise they pause the
dispatcher too.

## Tests In Files In Scope

A story that changes behavior also changes the tests that assert the old
behavior, so those tests are in scope by definition. The planner, or the tpm
when it writes the Work Packet, lists the affected test files or the `tests/`
directory in Files in scope. The work-claim gate treats every file under a
directory entry as in scope, including a test file the implementer adds.

A Work Packet never tells the implementer to stop because a test outside
Files in scope must change, in that wording or any other. The validator's
advisory rationalization check flags that instruction only in a planner
handoff with a completion status, so an ordinary assignment goes unchecked;
the rule holds either way. When the planner cannot tell which test files
assert the behavior, it lists `tests/`.

To find which tests assert the old behavior, the planner may run the relevant
tests read-only while scoping, when its launch permits the command. For a
Claude role that keeps the default allowlist, the top-level
`interpreter_grants: true` in the dispatch config supplies the `python`,
`python3`, and `pytest` grants; agy roles get the same grants from that key.
Without a grant, or when the launch refuses the command, the planner reads
the tests instead and lists `tests/`. Scoping runs change no files, and this
rule changes no role's permissions.

## When To Flag Uncertainty

Stop and route to `planner`, `validator`, or `user` when scope, ownership,
routing, tests, or Git state are ambiguous. Do not widen your role boundary to
avoid asking.

## Escalation Protocol

Use `status: escalate_to_user` and `next_agent: user` when human input is
required. Use `blocked_missing_context` when the missing input is specific and
the next human question is clear. Use `blocked_implementation_failure` when an
implementation path failed structurally and needs re-scoping.

Each blocking status already names who acts next, so set `next_agent` to
match it:

- `blocked_missing_context` and `escalate_to_user` pause for a person. Pair
  them with `next_agent: user` and state the one question that person must
  answer.
- `blocked_implementation_failure` goes to the planner. Pair it with
  `next_agent: planner`. Use it when the Work Packet cannot be completed as
  written, a work-claim gate failed, or the assignment needs re-scoping. A
  `tpm` also sends it to `planner` when the next story is missing, ambiguous,
  stale, or needs a design decision.

To ask the planner for a re-scope, use `blocked_implementation_failure`, not
`blocked_missing_context`.

**Reconciled user escalations.** When the dispatch config sets
`reconcile_user_escalations: true`, a user escalation may be reconciled once
before the dispatcher pauses: a frontmatter `next_agent: user` handoff with
`escalate_to_user` or `blocked_missing_context` from a role other than
`planner` gets one bounded planner check per handoff content per run. The
planner rewrites it to `user` with the same question when the blocker still
holds, or to the next role when it does not. A planner-written user route
gets no check and pauses as `reconciled by planner`. With the default
`false`, every user route pauses at once. The pass is not proposal approval
(see Work Intake below).

**Environment refusal handbacks.** An implementer handback with
`status: blocked_implementation_failure` and an `Environment refusal` bullet
reports that the environment, not the code, stopped the work. Never route
the same role an unchanged Work Packet after one: the same refusal stops it
again. The validator's advisory `planner_redispatch_after_refusal` warning
flags that re-dispatch, but the rule holds either way. The planner takes one
of two paths:

- **Gate-verified re-dispatch.** When the refused command is the Work
  Packet's Verification command and the work-claim gate is on
  (`enforce_work_packet: true`; dispatcher commit mode always runs the gate),
  revise the Work Packet's Context or Objective to instruct the
  Gate-Verified Handback above. Changing only the `reason` or the Next Step
  leaves the Work Packet unchanged. Keep the Verification command exactly as
  written, so the gate still runs it. The instruction requires a refusal
  from the implementer's current turn and all five Verification Evidence
  fields, with `Commands run` beginning `deferred to the work-claim gate:`
  and quoting the refusal, and an `Output summary` that identifies it. A
  failing test is not an environment refusal, and the instruction never
  authorizes PASS evidence for a command that did not pass.
- **Route to `user`.** When the gate is off (`enforce_work_packet: false`)
  or cannot address the blocker, such as a refused command other than the
  Verification command, route to `user` with `status: escalate_to_user` and
  the one remedy for the reported refusal. For a `permission rule` refusal
  of `python`, `python3`, or `pytest` on a default allowlist, name
  `interpreter_grants: true`; otherwise name the exact rule, such as an
  `allowed_tools` entry `Bash(npm test:*)` for a Claude role or the agy
  `permissions.allow` grant. An allowlist setting cures neither a
  `missing binary` nor a `no TTY` refusal: for a `missing binary`, name the
  executable the operator must install or put on `PATH`; for `no TTY`, name
  the interactive step the operator must run or the non-interactive form of
  the command.

## Operator Stories

An operator story is a story whose edits only a person can make, such as
edits under `.claude/` that Claude roles cannot write. A planner or tpm that
routes an operator story to `user` first writes the edits as an artifact the
operator can apply, then names it in the handoff. A prose description of the
edits, or a fenced diff in the handoff with no artifact behind it, does not
satisfy this contract: edits copied out of Markdown carry stray fence lines
and miss changes.

- **Location.** Write the artifact under
  `logs/dispatch/operator/<story id>/`. `logs/` is git-ignored: never stage
  or commit the artifact, and do not list it in a Work Packet's Files in
  scope.
- **Format.** Use one of two forms. Complete file contents: one file per
  target, holding the full new text of that file, at
  `logs/dispatch/operator/<story id>/files/<target path>`. Or one applyable
  patch, `logs/dispatch/operator/<story id>/changes.patch`, a unified diff
  that `git apply` accepts from the repository root. Prefer complete file
  contents; they apply without matching context.
- **Command block.** Give one command block that the operator copies and runs
  from the repository root to apply the artifact. Write it for the operator's
  shell: `Copy-Item -LiteralPath <artifact> -Destination <target>` in
  PowerShell, `cp <artifact> <target>` in a POSIX shell, or
  `git apply logs/dispatch/operator/<story id>/changes.patch`, which runs the
  same in both. The block holds only commands, never file text, so no fence
  label such as `python` or `diff` can land in a file. It may end by staging
  and committing exactly the target files; it never stages
  `docs/handoff/HANDOFF.md`.
- **Plain reply.** Tell the operator that once the edits are applied and
  committed, they may replace `docs/handoff/HANDOFF.md` with a plain sentence
  such as `I made the edits.` A person's reply needs no frontmatter; the
  planner reads it, checks the operator's commits, and writes the next valid
  handoff. Every agent-authored handoff still follows the routing contract
  above. See "Editing HANDOFF.md In Plain Words" below for what happens next.

## Editing HANDOFF.md In Plain Words

A person may replace `docs/handoff/HANDOFF.md` with plain words at any pause,
for example `I made the edits.` Frontmatter is optional for a person. This
exception covers only a person's edit: every handoff an agent writes still
needs valid routing frontmatter.

The dispatcher spots a person's edit by comparing the file with the last
version it or a dispatched role wrote, so make the edit while the dispatcher
is running, for example while it waits at a pause. The first version a run
sees is its starting point, not an edit: a file replaced while the dispatcher
was stopped is not treated as a person's edit.

- **Valid frontmatter.** An edit whose frontmatter passes validation routes as
  written.
- **No, unparseable, or invalid frontmatter.** The dispatcher sends the
  planner once to interpret the edit. The planner gets the person's text,
  quoted, and the last valid handoff the dispatcher saw before the edit, when
  it saw one. It checks that context and the Git state: the branch, the
  uncommitted files, and the commits since that handoff. It then rewrites
  only `HANDOFF.md` into a valid handoff for what the person meant. It does
  not carry out the request itself, and its handoff passes the same
  validation as any other. When the intent is unclear, it routes to `user`
  with `status: escalate_to_user` and one plain question. A request for new
  work becomes a Work Intake proposal; the pass never approves one.
- **A failed interpretation.** If that pass leaves a handoff with only
  formatting errors, such as a `## Verification Evidence` block with the
  wrong labels, the planner gets one repair pass for them. If the handoff
  still does not pass validation, the dispatcher pauses and does not retry
  further. Edit the file again or answer at the operator prompt.
- **Role output.** An invalid handoff a role wrote is not a person's edit and
  keeps its existing repair path.

When the previous handoff routed an operator story to `user`, a note such as
`I made the edits.` does not prove the story is done. The planner checks the
work with read-only Git commands:

- the operator's commits make each requested edit and no other change;
- no operator commit changes `docs/handoff/HANDOFF.md`;
- no file other than `docs/handoff/HANDOFF.md` is uncommitted;
- only when publishing would move the operator's commit to a story branch
  (`publish.mode: pr` with `base_branch` and `branch_prefix` set): the
  checkout is on the base branch, exactly one non-merge commit ahead of its
  fork point with the remote base branch. Below that commit there may also
  be unpublished non-merge commits that change only `PROJECT_STATE.md` and
  `COMPLETED_WORK.md`, such as an epic close the finalizer left on the base
  branch; the pull request carries them. In every other case the commits
  are audited where they are.

A clean result routes to `auditor` with `status: ready_for_review` and the
concrete `git rev-parse HEAD` SHA as `scope_sha`. Anything off routes back to
`user`, naming each problem and the exact fix in plain words. So apply the
artifact, commit exactly the requested edits, keep the artifact and
`docs/handoff/HANDOFF.md` out of the commit, and only then leave the note.

## Work Intake

A person may ask for new work in plain words, typed at the operator prompt
during a pause. The planner turns the request into a reviewable plan:

- **Proposal.** A `## Proposed Backlog` section gives each proposed epic's
  intro, goal, boundaries, `Fit:` line, and story outline. Story outlines
  are planner scope; sizing and splitting stay with the tpm when configured.
- **Drafts change only the handoff.** A proposal or revision routes to
  `user` with `status: escalate_to_user` and carries no Work Packet. It
  edits no file but `HANDOFF.md`: no project state file edits and no Linear
  issues. Active work and `remaining_stories` carry forward unchanged.
- **Revision.** Typed guidance on a pending proposal revises it and routes
  to `user` again, with no automatic revision limit.
- **Approval.** Only an explicit dispatcher instruction saying the operator
  approved the proposed backlog is approval. The best-judgment request,
  typed guidance, a plain-words interpretation pass, and a user escalation
  reconciliation pass are not. On approval the planner records the approved
  epics in the configured project state file (`project_state_path`), keeps
  existing open work, and opens the next epic by fit: it routes to `tpm`
  when `agents.tpm` is configured, otherwise it writes the Work Packet. In
  dispatcher commit mode it leaves that edit for the dispatcher and never
  stages or commits it. Outside approved intake, new stories are still
  recorded through the next Work Packet.
- **Linear mode.** The same approval also lets a Linear-mode planner create
  one issue per approved story outline through its own connector, by the
  generated `## Linear Mode: planner` rules; the dispatcher never calls
  Linear, and drafts make no Linear change. The planner then routes to `tpm`
  without leasing, or without a tpm leases the first issue. A failed or
  unconfirmed creation is reported, never claimed, and pauses for the user.
- **No unblocked story.** A planner that routes to `user` because no open
  epic has an unblocked story invites a plain-language request at the
  operator prompt instead of asking for hand edits.

## CHANGELOG Entries

`CHANGELOG.md` grows with every story, and reading it whole costs tokens on
every dispatch that edits it. To add an entry, read only the top of the file,
about the first 40 lines (Claude `Read` with a `limit`, `agy` `view_file` with
a line range, Codex `head -n 40 CHANGELOG.md` in a POSIX shell or
`Get-Content CHANGELOG.md -TotalCount 40` in PowerShell), and add the new
bullet as the first entry under `## Unreleased`. Read further only when the
Work Packet asks you to change an older entry.

## Common Failure Modes

- Missing or malformed YAML frontmatter.
- Provider names used as public workflow roles.
- `scope_sha: HEAD` instead of a concrete SHA.
- Completion claims without `## Verification Evidence`.
- Planner assignments without a concrete work packet. A Work Intake
  proposal or revision routed to `user` carries none.
- A `Verification command` written as prose, such as `` `cmd1`, then
  `cmd2` ``, instead of one command per nested list item.
- A `Verification command` that points `--basetemp`, `TMP`, or `TEMP`
  inside the repository to work around a sandbox temp-directory error.
- Auditor approvals that skip spec compliance.
- Repeated implementer/auditor bounces on the same story.
- Completion claims whose `scope_sha` is not HEAD or whose diff leaves the
  Work Packet scope.

Verification-only work, such as re-running tests or checking an earlier
commit, belongs with the auditor, not in an implementer Work Packet. When the
work-claim gate runs, an implementer `ready_for_review` claim whose commits
since dispatch change no file other than the handoff fails `no_changes`, and
a claim whose `Commit SHA verified` names a different commit than `scope_sha`
fails `evidence_sha_mismatch`.
