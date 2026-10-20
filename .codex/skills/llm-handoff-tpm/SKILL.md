---
name: "llm-handoff-tpm"
description: "Use for tpm work in repositories that use llm-handoff: pick the next story inside the open epic and write its Work Packet in HANDOFF.md."
---

# llm-handoff TPM Skill

Use this skill when Codex is acting as the `tpm` role in an `llm-handoff` workflow. You pick the next story inside the epic the planner opened and rewrite `docs/handoff/HANDOFF.md` with its assignment. You do not implement code, audit work, or choose epics.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared frontmatter, status, evidence, and escalation contract.

The tpm role exists only when `agents.tpm` is configured in the dispatch config. Without it, the planner picks every story itself and nothing routes to `tpm`.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. The project state file (`PROJECT_STATE.md` unless the dispatcher prompt or `AGENTS.md` names another path)
4. `docs/handoff/HANDOFF.md`
5. Only the code needed to scope the story

## Role Boundary

- Take the next story from `remaining_stories` and write its assignment with a concrete `## Work Packet`.
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

## Picking The Story

1. Take the first entry of `remaining_stories`.
2. Find that story in the project state file and read the code it touches.
3. Write the assignment: set `story_id` and `story_title` to that story, remove only that entry from `remaining_stories`, and copy every other entry forward unchanged.
4. Add the `## Work Packet` below and route to the implementer that owns the work.

## Story List And Project State

- Never drop a `remaining_stories` entry. A tpm removes only the story it assigns as `story_id`. A story that looks stale, already done on the default branch, or wrong routes to `planner` with the reason; never write a `## Dropped Stories` section yourself.
- Do not edit the project state file yourself. A Work Packet may put the project state file in Files in scope only so the implementer records the assigned story's progress in its Current Status.
- Never add, drop, split, renumber, or reorder stories or epics. Route any of those to `planner`.
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
- two stories compete and their order matters;
- the next story conflicts with the project state file;
- the next story looks stale or already done on the default branch;
- the next story needs a design decision.

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
