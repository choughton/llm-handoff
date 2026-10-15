---
name: "llm-handoff-finalizer"
description: "Use for finalizer work in repositories that use llm-handoff: record an approved epic close in durable project state and route the next cycle."
---

# llm-handoff Finalizer Skill

Use this skill when Codex is acting as the `finalizer` role in an `llm-handoff` workflow. The dispatcher runs the finalizer after an approved epic close.

Your job is to record the closed scope in durable project state, clear the finalizer route from `docs/handoff/HANDOFF.md`, and return machine-readable output. You do not audit or implement code.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared frontmatter, status, and evidence contract.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. `docs/handoff/HANDOFF.md`
4. `PROJECT_STATE.md` if present
5. `git log --oneline -10`

## Preconditions

Proceed only when the handoff clearly shows:

- an approved audit or equivalent approval signal
- `next_agent: finalizer`
- `close_type: epic`
- a concrete `scope_sha`

If any precondition is missing, do not update project state. Rewrite `docs/handoff/HANDOFF.md` to route to `validator` or `user` and return `PROJECT STATE UPDATED: NO`.

## State Update

If `PROJECT_STATE.md` exists, update it according to the consuming repository's local instructions. Keep the edit focused on the closed scope, next active scope, and durable commit references.

If `PROJECT_STATE.md` does not exist, create a minimal one only when `AGENTS.md` or `docs/handoff/HANDOFF.md` explicitly says this repository uses it. Otherwise, leave project state unchanged and route to `planner` or `user` with a clear reason.

## Handoff Rewrite

Rewrite `docs/handoff/HANDOFF.md` so the next cycle does not route back to `finalizer`. Every handoff write must begin with a YAML frontmatter block:

```yaml
---
next_agent: <enum>       # planner | backend | frontend | auditor | validator | finalizer | user
reason: <string>         # quote every `reason` value
epic_id: <string>
story_id: <string>
story_title: <string>
remaining_stories:
  - <story id/title>
status: <verified_pass | blocked_missing_context | escalate_to_user>
scope_sha: <7-40 hex SHA>
prior_sha: <optional prior SHA>
producer: finalizer
---
```

Prefer:

```yaml
---
next_agent: planner
reason: "Scope closed; planner should select the next bounded assignment."
status: verified_pass
scope_sha: <finalizer commit SHA after commit>
producer: finalizer
---
```

Keep `epic_id`, `story_id`, `story_title`, and `remaining_stories` from the previous handoff when they are known. Use `next_agent: user` if no next scope can be inferred safely.

Run `git rev-parse HEAD` for concrete SHAs. Do not write `scope_sha: HEAD`.

## Commit And Push

- Commit only the state and handoff files required for the finalizer update.
- Use a clear commit message and any trailer required by `AGENTS.md`.
- Never run `git push`; remote pushes are the operator's call. Report `PUSH RESULT: SKIPPED`.

## Git Inside The Sandbox

If the dispatcher's instruction says dispatcher commit mode is on, do not
stage or commit at all: leave your changes in the working tree and follow the
dispatcher's instruction for what to report.

Otherwise, Codex keeps `.git` read-only inside its sandbox, so `git add` and
`git commit` fail there. The repository's `.codex/rules/llm-handoff.rules`
does not let git writes out of the sandbox, and it forbids `git push`. If you
are sandboxed and the protocol asks you to commit, do not work around the
sandbox: leave the changes uncommitted, do not invent a commit SHA, and say
under `CHANGES MADE` that the sandbox blocked the commit and that the operator
should enable `commit_mode: dispatcher` or commit the files.

Read-only git commands (`status`, `diff`, `log`, `rev-parse`) stay in the
sandbox. On Windows the sandbox runs as a separate account, so add
`-c safe.directory=<repository root>` to those if git reports dubious
ownership.

## Required Output

Return only this machine-readable format, one field per line:

```text
PROJECT STATE UPDATED: YES | NO
HANDOFF.MD REWRITTEN: YES | NO
SCOPE CLOSED: <scope name>
NEXT ROUTE: <planner | user | another supported next_agent>
AUDIT SHA: <full or short sha>
COMMIT SHA: <single full or short sha for the PROJECT_STATE.md/HANDOFF.md commit>
PUSH RESULT: SKIPPED | PUSHED | FAILED (<optional detail>)
CHANGES MADE:
  - <file>: <what changed>
```

Do not return prose, markdown headings, or conversational text outside the required format.
