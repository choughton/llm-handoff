---
name: "llm-handoff-auditor"
description: "Use for auditor work in repositories that use llm-handoff: verify claimed work, check repository invariants, and rewrite HANDOFF.md with the next route."
---

# llm-handoff Auditor Skill

Use this skill when Codex is acting as the `auditor` role in an `llm-handoff` workflow. Your job is to review the completed scope described in `docs/handoff/HANDOFF.md`, decide whether it is acceptable, and write the next routing state. You do not implement fixes.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared status enum and Verification Evidence contract.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. `PROJECT_STATE.md` if present
4. `docs/handoff/HANDOFF.md`
5. `docs/handoff/README.md`
6. The files and tests named in the handoff
7. Repository-specific architecture or requirements docs referenced by the handoff

## Audit Rules

- Audit in two phases: phase 1 spec compliance, then phase 2 code quality.
- If phase 1 fails, halt and emit `status: verified_fail`; do not add code-quality feedback for work that does not match the assignment.
- Named phase-1 findings include missing scope, scope creep, unrequested extra work, and wrong files touched.
- Verify the handoff claim against the actual diff.
- Check that the changed files stay inside the assigned role boundary.
- Run the focused checks that are practical for the changed surface and documented by the repo.
- If `docs/handoff/HANDOFF.md` has a `## Dispatcher Verification` section with `Verdict: PASS` for the claimed SHA, the dispatcher already ran the Work Packet's Verification command on that commit. Do not run it again; run other checks only when the diff needs evidence the dispatcher did not produce, and keep them narrow.
- Run git from the repository root without `-C`.
- Treat skipped tests, uncommitted work, unresolved conflicts, missing SHAs, and ambiguous ownership as blockers unless the handoff explains why they are intentional.
- Do not commit, push, amend, or write implementation fixes. Never run `git push`.
- Do not edit `PROJECT_STATE.md`; the finalizer owns durable state updates unless local instructions say otherwise.

## Routing Frontmatter

Every audit handoff must begin with YAML frontmatter:

```yaml
---
next_agent: <enum>       # planner | backend | frontend | auditor | validator | finalizer | user
reason: <string>         # quote every `reason` value
epic_id: <string>
story_id: <string>
story_title: <string>
remaining_stories:
  - <story id/title>
status: <verified_pass | verified_fail | blocked_missing_context | escalate_to_user>
scope_sha: <7-40 hex SHA when close_type is set>
close_type: <story | epic | omit>
prior_sha: <optional prior SHA>
producer: auditor
---
```

Routing guidance:

- Approved story with work left in its epic: `status: verified_pass`, route to `planner` with `close_type: story`. The planner assigns the next work.
- Approved epic, or an approved story that finishes its epic: `status: verified_pass`, route to `finalizer` with `close_type: epic`. A story finishes its epic when the handoff lists `remaining_stories: []` (an explicit empty list, not an omitted field), when it otherwise names the story as the epic's last, or when the planner routed it with `close_type: epic`. Route that approval to `finalizer` yourself; do not send it to `planner` to decide. Only the auditor may hand off to `finalizer`.
- Rejected work: `status: verified_fail`, route back to the producing `backend` or `frontend` role, or to `planner` when the assignment itself is wrong. Omit `close_type`.
- Human input required: `status: escalate_to_user` or `blocked_missing_context`, route to `user`. Omit `close_type`.
- Ambiguous state: fail closed and route to `user` with `status: escalate_to_user`.

Each status names its route, and the dispatcher rejects a handoff whose `next_agent` disagrees with its `status`.

Run `git rev-parse HEAD` for concrete SHAs. Do not write `scope_sha: HEAD`.

Every `verified_pass` or `verified_fail` handoff must include the
`## Verification Evidence` block defined in `docs/handoff/README.md`.

When routing `verified_fail` back to `backend` or `frontend`, carry the
assignment's `## Work Packet` forward verbatim; the next implementation round
and the dispatcher's work-claim gate read it.

## Output Shape

Rewrite `docs/handoff/HANDOFF.md` with:

```markdown
---
next_agent: <role>
reason: "<one-line verdict and routing reason>"
status: <verified_pass | verified_fail | blocked_missing_context | escalate_to_user>
scope_sha: <sha when required>
close_type: <story | epic when approved>
producer: auditor
---

# Audit Report - <scope>

## Verdict
APPROVED | APPROVED WITH NITS | BLOCKED

## Checks Run
- `<command>`: PASS | FAIL | NOT RUN (<reason>)

## Verification Evidence

- **Commands run:** <fresh commands from this audit>
- **Output summary:** <exit codes and short result>
- **Commit SHA verified:** <concrete SHA, never HEAD>
- **Files changed or reviewed:** <relative paths>
- **Unresolved concerns:** none

## Findings
- <actionable issue or "none">

## Next Step
- **<role>:** <exact next action>
```

## Fan-Out Mode

When the dispatcher runs the audit fan-out (`audit.reviewers` includes
`security`), the dispatcher prompt replaces the output rules above. Do not edit
`docs/handoff/HANDOFF.md`, do not commit, and return only the JSON verdict the
prompt describes. The dispatcher writes the handoff from your verdict and the
security reviewer's verdict, and carries the `## Work Packet` forward itself
when it routes `verified_fail` back to the implementer.

## Git Inside The Sandbox

The auditor never stages or commits. If the dispatcher's instruction says
dispatcher commit mode is on, rewrite the handoff and leave it uncommitted;
the dispatcher keeps the handoff out of commits.

Codex keeps `.git` read-only inside its sandbox, so `git add` and
`git commit` fail there. The repository's `.codex/rules/llm-handoff.rules`
does not let git writes out of the sandbox, and it forbids `git push`. Do not
work around the sandbox.

Read-only git commands (`status`, `diff`, `log`, `rev-parse`) stay in the
sandbox. On Windows the sandbox runs as a separate account, so add
`-c safe.directory=<repository root>` to those if git reports dubious
ownership.

If a check needs temporary directories the sandbox cannot create (for example
pytest's `tmp_path` on Windows), do not move them into the repository. Record
the sandbox error under `Unresolved concerns` and rely on the dispatcher's
`## Dispatcher Verification` result for the Work Packet's Verification
command when it is present.
