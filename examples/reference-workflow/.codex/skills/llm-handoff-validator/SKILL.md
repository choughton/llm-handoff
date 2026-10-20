---
name: "llm-handoff-validator"
description: "Use for validator work in repositories that use llm-handoff: validate HANDOFF.md routing state, or rewrite ambiguous routing with a deterministic next_agent."
---

# llm-handoff Validator Skill

Use this skill when Codex is acting as the `validator` role in an `llm-handoff` workflow.

The `validator` role covers two tasks:

- **Handoff validation:** inspect `docs/handoff/HANDOFF.md` and report whether the dispatch loop can continue safely. This task modifies no files.
- **Handoff routing:** when the dispatch loop could not determine a safe route, rewrite `docs/handoff/HANDOFF.md` with a deterministic `next_agent` value.

The dispatcher's prompt names which task applies. Do only that task. If the prompt does not say which one applies, run handoff validation and modify no files.

Neither task implements code, audits code quality, updates durable project state, commits, or pushes. Never run `git push`.

Start by reading `docs/handoff/HANDBOOK.md`; it defines the shared frontmatter, status, and evidence contract.

## Read First

1. `docs/handoff/HANDBOOK.md`
2. `AGENTS.md`
3. `docs/handoff/HANDOFF.md`
4. The project state file (`PROJECT_STATE.md` unless the dispatcher prompt or `AGENTS.md` names another path), if present and needed for close-type validation or to distinguish story close from epic close

## Handoff Validation

Validate these items in order:

1. YAML frontmatter exists at the top of `docs/handoff/HANDOFF.md`.
2. Frontmatter parses as YAML.
3. `next_agent` is one of `planner`, `backend`, `frontend`, `auditor`, `validator`, `finalizer`, or `user`.
4. `reason` is present, non-empty, and quoted when it contains punctuation that could break YAML.
5. `close_type`, when present, is `story` or `epic`.
6. `scope_sha` is present when `close_type` is set.
7. `scope_sha` and `prior_sha`, when present, are 7-40 character hex strings and resolve with `git cat-file -t <sha>`.
8. `finalizer` routing is used only with `close_type: epic`.
9. `status`, when present, is one of the canonical enum values in the handbook.
10. Completion statuses include the `## Verification Evidence` block.
11. The handoff body contains enough detail to act on: files, checks, findings, or acceptance criteria.
12. The current git state is compatible with the handoff claim. Report dirty state as WARN unless local instructions require clean state.
13. When `docs/handoff/HANDBOOK.md` contains a `## Linear Mode: validator` section, run that section's evidence check read-only. Report each mismatched field on the `LINEAR-EVIDENCE` row as `FAIL`, naming both the comment value and the handoff value. If the issue or its comments cannot be read, report `FAIL - cannot read the leased issue`. Without that handbook section, report `PASS - Linear mode is off`. Never edit a comment, the issue, or `HANDOFF.md` for this check.

Return exactly:

```text
VALID: YES | NO | WARNINGS-ONLY
CHECKS:
  FRONTMATTER:    PASS | WARN | FAIL - <detail>
  SHA-PRESENT:    PASS | WARN | FAIL - <detail>
  SHA-FRESH:      PASS | WARN | FAIL - <detail>
  ROUTING:        PASS | WARN | FAIL - <detail>
  CONTENT:        PASS | WARN | FAIL - <detail>
  GIT-STATE:      PASS | WARN | FAIL - <detail>
  LINEAR-EVIDENCE: PASS | WARN | FAIL - <detail>
SUMMARY: <one sentence>
BLOCKERS: <numbered list if VALID=NO, otherwise "none">
```

Only FAIL makes `VALID: NO`. WARN-only results should use `VALID: WARNINGS-ONLY`.

During handoff validation, do not edit `docs/handoff/HANDOFF.md` and do not re-route the work.

## Handoff Routing

The public `next_agent` enum is:

- `planner`
- `backend`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Use these rules:

- Backend/data/CLI/integration implementation goes to `backend`.
- UI/frontend implementation goes to `frontend`.
- Planning, scope decomposition, or next-story assignment goes to `planner`.
- Completed implementation that needs review goes to `auditor`.
- Approved final scope goes to `finalizer` only when `close_type: epic` is correct.
- Broken, malformed, or internally inconsistent handoff state goes to `validator`.
- Missing human decision, missing credentials, or unsafe ambiguity goes to `user`.

If the handoff mentions a provider-specific name such as Codex, Antigravity, or Claude, translate it to the public role it is serving in this repository.

Rewrite `docs/handoff/HANDOFF.md` with a valid frontmatter block at the top. Preserve useful context from the previous handoff below the new routing block.

```yaml
---
next_agent: <planner | backend | frontend | auditor | validator | finalizer | user>
reason: "<short reason; quote every `reason` value>"
epic_id: <string if known>
story_id: <string if known>
story_title: <string if known>
remaining_stories:
  - <story id/title>
status: <canonical status when applicable>
bounce_count: <optional retry count>
evidence_present: <optional boolean>
scope_sha: <7-40 hex SHA when close_type is set>
close_type: <story | epic | omit>
prior_sha: <optional prior SHA>
producer: validator
---
```

Run `git rev-parse HEAD` for concrete SHAs. Do not write `scope_sha: HEAD`.

Do not guess. When route evidence is insufficient, set `next_agent: user` and ask one concrete question in the body.

After rewriting the handoff, return a short summary:

```text
ROUTING UPDATED: YES
NEXT_AGENT: <role>
REASON: <one sentence>
```

## Git Inside The Sandbox

The validator never stages or commits. If the dispatcher's instruction says
dispatcher commit mode is on, leave any handoff rewrite uncommitted; the
dispatcher keeps the handoff out of commits.

Codex keeps `.git` read-only inside its sandbox, so `git add` and
`git commit` fail there. The repository's `.codex/rules/llm-handoff.rules`
does not let git writes out of the sandbox, and it forbids `git push`. Do not
work around the sandbox.

Read-only git commands (`status`, `diff`, `log`, `rev-parse`, `cat-file`)
stay in the sandbox. On Windows the sandbox runs as a separate account, so add
`-c safe.directory=<repository root>` to those if git reports dubious
ownership.
