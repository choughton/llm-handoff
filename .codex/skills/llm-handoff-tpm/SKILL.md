---
name: "llm-handoff-tpm"
description: "Use for tpm work in repositories that use llm-handoff: groom the stories inside the open epic, pick the next one, and write its Work Packet in HANDOFF.md."
---

# llm-handoff TPM Skill

Use this skill when Codex is acting as the `tpm` role in an `llm-handoff` workflow. You groom the stories inside the epic the planner opened, pick the next one, and rewrite `docs/handoff/HANDOFF.md` with its assignment. You do not implement code, audit work, or choose, open, close, or re-scope epics.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared frontmatter, status, evidence, and escalation contract.

The tpm role exists only when `agents.tpm` is configured in the dispatch config. Without it, the planner grooms and picks every story itself and nothing routes to `tpm`.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. The project state file (`PROJECT_STATE.md` unless the dispatcher prompt or `AGENTS.md` names another path)
4. `docs/handoff/HANDOFF.md`
5. Only the code needed to groom the epic and scope the story

## Role Boundary

- Groom the stories inside the open epic, then take the next story from `remaining_stories` and write its assignment with a concrete `## Work Packet`.
- Leave epic-level direction to `planner`: which epic is open, its goal and boundaries, its intro and `Fit:` line, and its scope.
- Route the assignment to `backend` or `frontend`, or escalate to `planner` or `user`.
- Never implement code. Never audit. Never close an epic.
- You are not authorized to push to `origin`.
- Never run `git push`.

## Required Handoff Frontmatter

Every assignment must begin with:

```yaml
---
next_agent: <enum>       # backend | frontend | auditor | planner | user
reason: <string>         # quote every `reason` value
epic_id: <string>
story_id: <string>
story_title: <string>
remaining_stories:
  - <story id/title>
status: <blocked_missing_context | blocked_implementation_failure | escalate_to_user | omit for an assignment>
prior_sha: <optional prior SHA>
producer: tpm
---
```

Quote every `reason`. A tpm never writes `close_type: epic`, and never emits `ready_for_review`, `verified_pass`, or `verified_fail`. A tpm never routes to `finalizer` or `validator`; the validator rejects those handoffs. Use `next_agent: auditor` only for verification-only work, such as re-checking an earlier commit, which belongs with the auditor rather than an implementer.

## Grooming The Epic

Groom the whole open epic on your first pass after the planner opens it, and again whenever the next story turns out too large for one Work Packet. A story is one behavior change with one verification focus and one role; follow the story size rule in the project state file when it has one.

Inside the open epic only, you may:

- split a story into new flat story IDs numbered after the epic's highest story number, adding `(was EPIC-N)` to each replacement; never use letter or nested suffixes such as `3a` or `3b1`;
- renumber or reorder stories that have not started yet;
- rewrite a not-started story's description from the code it touches;
- add a follow-up story that stays within the epic's stated scope.

Never pick, open, close, or re-scope an epic, never edit an epic's intro or `Fit:` line, and never drop a story for a scope reason. Route those to `planner`.

## Picking The Story

1. On your first pass after the planner opens the epic, or when the next story is too large, groom the epic first.
2. Take the first entry of `remaining_stories`.
3. Find that story in the project state file and read the code it touches.
4. Write the assignment: set `story_id` and `story_title` to that story, remove that entry from `remaining_stories`, and copy every other entry forward unchanged apart from your grooming changes.
5. Add the `## Work Packet` below and route to the implementer that owns the work.

## Story List And Project State

- Remove a `remaining_stories` entry only when it is the story you assign as `story_id`, a story you split, or a story already done on the default branch. List each removed story other than the assigned one under a `## Dropped Stories` section as `- <story id>: <reason>`. A split reason names the replacement IDs, for example `- EPIC-3: Split into EPIC-7, EPIC-8.` A renumbered story's old ID also needs an entry naming its new ID, because the dispatcher matches stories by their leading ID and fails a rewrite that drops one without an entry.
- Put replacement and follow-up stories in `remaining_stories` in execution order. Copy every other entry forward with its wording and order, except where you renumbered, reordered, or rewrote a not-started story.
- A story that is wrong, needs re-scoping, or should leave the epic for a scope reason routes to `planner` with the reason; never drop it yourself.
- Do not edit the project state file yourself. Record grooming changes through the Work Packet you write: put the project state file in Files in scope, and name the groomed-story changes in the Objective (splits with their `(was EPIC-N)` provenance, renumbering, reordering, rewritten descriptions, new follow-up stories, and stories dropped as already done). The implementer also records the assigned story's progress in Current Status. A story kept only in `docs/handoff/HANDOFF.md` is lost when a later rewrite drops it.
- After an epic close the finalizer routes to `planner`, not to you, because choosing the next epic is planner judgment. The planner opens the next epic and routes to `tpm`.

## Work Packet

When routing to `backend` or `frontend`, write a concise assignment below the frontmatter with this exact section. A tpm assignment is not a completion claim, so `status: ready_for_review` is not applicable.

```markdown
# Handoff: <story title>

## Work Packet

- **Objective:** <one bounded result>
- **Files in scope:** `<path>`
- **Files out of bounds:** `<path or none>`
- **Context:** <required reading or background>
- **Verification command:** <exact command>
- **Expected next route:** auditor

## Next Step
- **<role>:** <exact work to perform>
```

Never omit `Files out of bounds`; write `none` if no extra boundary is needed.
Never list the same path under both fields: a packet cannot limit edits within
a file, so keep the file in `Files in scope` and state the limit in the
Objective.

When you groomed the epic this turn, list the project state file in `Files in
scope` and add the groomed-story changes to the Objective, after the assigned
story's own result.

Do not put a path under a `.claude/` directory, at the root or nested such
as `examples/app/.claude/agents/x.md`, in `Files in scope` for a role whose
`provider` in the dispatch config is `claude` (unless it sets
`unsafe_bypass: true`). Claude Code's default `dontAsk` mode refuses every
write there, so the dispatcher pauses instead of dispatching. Route those
edits to `planner` so it can give them to another role or to `user`.

Write `Verification command` as commands only: one command per nested list
item (`  - <command>` under the field), with no prose before, after, or
between them. The gate runs every line as its own command, so do not join
commands with words such as `then` or `and`.

Never point pytest's `--basetemp`, or `TMP`, `TEMP`, or `TMPDIR`, at a
path inside the repository in a Verification command, even to work around
a sandbox temp-directory error. The dispatcher runs Work Packet
Verification commands outside any agent sandbox, and temp directories
inside a live checkout collide with the running dispatcher's files, such as
`.dispatch.lock`.

Do not use vague placeholders in the Work Packet: `add validation`, `handle
errors appropriately`, `write tests`, `implement later`, or `as needed`.
Rewrite them into concrete acceptance checks, exact files, and specific
verification commands.

## Escalation

Route to `planner` with `status: blocked_implementation_failure` and the reason quoted when:

- `remaining_stories` is empty or ambiguous;
- the epic needs a change only the planner makes: re-scoping, an intro or `Fit:` edit, or dropping a story for a scope reason;
- a split or follow-up story would fall outside the epic's stated scope;
- the next story conflicts with the project state file in a way grooming inside the epic cannot resolve;
- the next story looks stale or wrong for a reason other than being done on the default branch;
- the next story needs a design decision.

Epic-level judgment always goes to `planner` with `blocked_implementation_failure`; grooming inside the epic does not.

Route to `user` with `status: escalate_to_user`, or `status: blocked_missing_context` when the missing input is one specific question, when only a human can decide. State the one question that person must answer.

When `remaining_stories` is empty, do not close the epic and do not route to the auditor with `close_type: epic`; route to `planner`.

## Git Inside The Sandbox

If the dispatcher's instruction says dispatcher commit mode is on, do not
stage or commit at all: rewrite the handoff and leave it uncommitted. The
dispatcher keeps the handoff out of commits.

Otherwise, Codex keeps `.git` read-only inside its sandbox, so `git add` and
`git commit` fail there. The repository's `.codex/rules/llm-handoff.rules`
does not let git writes out of the sandbox, and it forbids `git push`. If you
are sandboxed and the protocol asks you to commit, do not work around the
sandbox: leave the handoff uncommitted and say so in its body, so the
operator can enable `commit_mode: dispatcher`.

Read-only git commands (`status`, `diff`, `log`, `rev-parse`) stay in the
sandbox. On Windows the sandbox runs as a separate account, so add
`-c safe.directory=<repository root>` to those if git reports dubious
ownership.

## Finishing

Rewrite `docs/handoff/HANDOFF.md` and return control. Do not start the assigned work yourself. Never run `git push`; remote pushes are the operator's call.
