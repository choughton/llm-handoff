---
next_agent: planner
reason: "W0.1 approved at story level; route to planner to choose the next SELFHOST-P0 story."
epic_id: "SELFHOST-P0"
story_id: "W0.1"
story_title: "Decouple the live handoff from template parity"
status: verified_pass
scope_sha: d74a3e0e85b6b27690baf369b0d85fd67995c4f4
close_type: story
producer: auditor
---

# Audit Report - W0.1 (Decouple the live handoff from template parity)

**Agent:** auditor
**Audit mode:** read-only fan-out (quality: claude, security: claude) on `c4ca7d78d61fe27b4ca1315040ced62eebfc83ab..d74a3e0e85b6b27690baf369b0d85fd67995c4f4`; the dispatcher wrote this handoff from the reviewers' structured verdicts.

## Verdict

APPROVED

## Merge Decision

- Quality reviewer verdict: pass
- Security reviewer verdict: pass
- Block severity threshold: high
- Blocking findings: 0
- Sub-threshold findings: 3

## Quality Review

W0.1 passes. The only change in the range is the one-line removal of docs/handoff/HANDOFF.md from COPIED_TEMPLATE_FILES. Nothing outside scope changed, and a fresh full suite passes (788 passed) with live dispatch state in the root HANDOFF.md.

### Audit Report - W0.1 Decouple the live handoff from template parity

#### Verdict
APPROVED WITH NITS

#### Phase 1: Spec Compliance
- **Objective met.** I ran `python -m pytest tests -q` fresh while the root `docs/handoff/HANDOFF.md` held live backend handback state (uncommitted working-tree change). All 788 tests passed, so the backend's sandbox-only failure (350 tmp_path PermissionError setup errors) does not happen outside the sandbox.
- **Change matches the Work Packet exactly.** `git diff c4ca7d7 d74a3e0` shows one deleted line: `"docs/handoff/HANDOFF.md",` was removed from `COPIED_TEMPLATE_FILES` (now starting at tests/test_reference_workflow.py:45).
- **Template starter coverage kept.** `test_reference_workflow_starter_handoff_routes_to_planner` (tests/test_reference_workflow.py:117) still reads `examples/reference-workflow/docs/handoff/HANDOFF.md` and asserts that it routes to planner with producer user.
- **Other parity entries kept.** All other `COPIED_TEMPLATE_FILES` entries (HANDBOOK, README, prompts, .codex, .gemini, .claude files) are still checked by `test_reference_workflow_copied_templates_match_source` (tests/test_reference_workflow.py:92-95).
- **Boundaries respected.** Commit d74a3e0 touches only `tests/test_reference_workflow.py`. Nothing changed under `llm_handoff/`, `examples/reference-workflow/`, `.claude/`, `.codex/`, `.gemini/` or `AGENTS.md`, and there is no production code change. The commit was made by the dispatcher in commit mode, as the handback's Rules section says.
- **No scope creep** and no extra work.

#### Phase 2: Code Quality
- The single deletion is the smallest correct fix. There are no new dependencies and the text is ASCII.
- Nit: the Work Packet cited the entry at tests/test_reference_workflow.py:43, but it was actually at :46 (inside the tuple starting at :45). This is only line drift in the plan and had no effect on the change.

#### Checks Run
- `git diff c4ca7d78..f5108780 --stat`: PASS (1 file, 1 deletion)
- `git diff c4ca7d7 d74a3e0` / `git show --stat d74a3e0`: PASS (only the assigned line removed)
- `python -m pytest C:/Users/hough/GitHub/llm-handoff/tests -q -p no:cacheprovider`: PASS (exit 0, 788 passed, 1 third-party warning)

#### Verification Evidence
- **Commands run:** see commands_run
- **Output summary:** git commands exit 0; pytest exit 0 with 788 passed and 1 warning
- **Commit SHA verified:** d74a3e0e85b6b27690baf369b0d85fd67995c4f4
- **Files changed or reviewed:** tests/test_reference_workflow.py, docs/handoff/HANDOFF.md, AGENTS.md
- **Unresolved concerns:** none

#### Findings
- info: The Work Packet line reference (:43) is off from the real entry line (:46). No action needed.
- info: Untracked paths that were already there before the change (`.w01-full-test-temp/`, `.w01-test-temp/`, `docs/assets/llm-handoff-concepts.png`) are not part of the scope. The user should clean them up or ignore them separately.

#### Next Step
- **planner:** Close W0.1 at story level (scope_sha d74a3e0e85b6b27690baf369b0d85fd67995c4f4) and pick the next SELFHOST-P0 story.

### Quality Findings

- [info] tests/test_reference_workflow.py:45 - Work Packet line reference drift
  - Description: The Work Packet cited COPIED_TEMPLATE_FILES at line 43, but the removed entry was at line 46 (tuple starts at line 45). This had no effect on the change.
  - Recommendation: No action needed. Planners can cite symbol names along with line numbers to make references sturdier.
- [info] docs/handoff/HANDOFF.md - Untracked scratch paths left over from earlier runs
  - Description: The backend handback reports untracked .w01-full-test-temp/, .w01-test-temp/ and docs/assets/llm-handoff-concepts.png, all present before this change. They are outside the scope and were not committed.
  - Recommendation: The user should delete or ignore the .w01-* temp directories outside the audit loop.

## Security Review

The range removes one entry from a template-parity list in a test file. It changes no runtime code and adds no security issues.

## Security Notes

- [info] tests/test_reference_workflow.py:45 - HANDOFF.md dropped from template byte-parity check
  - Description: The only change is that docs/handoff/HANDOFF.md is no longer in COPIED_TEMPLATE_FILES. The test still checks that the template exists (REQUIRED_TEMPLATE_FILES), but it no longer checks that the repository's live HANDOFF.md matches it byte for byte. That is expected while the repo hosts its own handoff. Nothing changes in the dispatcher's runtime, inputs, paths, subprocess calls, secrets handling, or dependencies. The other guardrail files (.claude/settings.json, the hooks, .codex rules, .gemini policies) are still covered by REQUIRED_TEMPLATE_FILES or COPIED_TEMPLATE_FILES as before.
  - Recommendation: No security action needed. Optionally, add a structural check for the live HANDOFF.md, such as required headings or fields, so it can't drift silently now that byte parity is gone.

## Verification Evidence

- **Commands run:** cd /c/Users/hough/GitHub/llm-handoff && git show c4ca7d7:docs/handoff/HANDOFF.md && echo ---- && git diff c4ca7d78d61fe27b4ca1315040ced62eebfc83ab d74a3e0e85b6b27690baf369b0d85fd67995c4f4 --stat; cd /c/Users/hough/GitHub/llm-handoff && git diff c4ca7d7 d74a3e0 && git show --stat d74a3e0 | head -20 && echo ---- && cat docs/handoff/HANDOFF.md && echo ---- && sed -n 30,60p tests/test_reference_workflow.py && sed -n 105,140p tests/test_reference_workflow.py; python -m pytest C:/Users/hough/GitHub/llm-handoff/tests -q -p no:cacheprovider; security reviewer: claude --permission-mode dontAsk --tools Read,Grep,Glob,Bash --strict-mcp-config --allowedTools Read Grep Glob Bash(git diff *) Bash(git show *) Bash(git log *) --disallowedTools Edit Write MultiEdit NotebookEdit WebFetch WebSearch --model claude-opus-5-5 --output-format json --json-schema <long-arg> --no-session-persistence -p <long-arg>
- **Output summary:** quality reviewer (claude) exit 0, verdict pass, 2 finding(s), 180.3s; security reviewer (claude) exit 0, verdict pass, 1 finding(s), 13.7s; git show/diff --stat: exit 0; 1 file changed (tests/test_reference_workflow.py), 1 deletion. git diff/show/cat/sed: exit 0; the diff removes only the "docs/handoff/HANDOFF.md" entry from COPIED_TEMPLATE_FILES, and commit d74a3e0 lists only tests/test_reference_workflow.py. pytest full suite: exit 0; 788 passed, 1 warning (third-party DeprecationWarning in google.genai), 114.25s.
- **Commit SHA verified:** d74a3e0e85b6b27690baf369b0d85fd67995c4f4
- **Files changed or reviewed:** tests/test_reference_workflow.py
- **Unresolved concerns:** none

## Next Step

- **planner:** W0.1 approved at story level; route to planner to choose the next SELFHOST-P0 story.

## Operator Note: Where Phase 1 Work Comes From

Phase 0 is complete. Pick the next bounded assignment from the planning notes
in `docs/proposals/`. That folder is untracked on purpose: read it, never
stage or commit it, and never copy private project names from it into the
repository.

- `docs/proposals/linear-onboarding-gap-analysis.md`: Phase 1 Work Packets
  W1.1-W1.13 (sections 3 and 5) and the Linear phases after them.
- `docs/proposals/next-audit-cost-controls.md`: Work Packet C1.
- `docs/proposals/next-antigravity-adapter.md`: Work Packets A1 and A2.

Some work landed outside the loop after those notes were written. Check the
code before assigning anything, and do not redo it:

- W1.7a (Claude role files and `init --provider claude`) is done.
- Dispatcher commit mode (`commit_mode: dispatcher`) is done; in that mode the
  gate already rejects claims with no new commit (`no_changes`), which is
  part of W1.11.
- Sign-in failures stop the loop with fix steps; agent launches keep the
  account name; the security reviewer schema fix, file-only usage logging,
  the parallel test run, and the passing-gate `## Dispatcher Verification`
  record are done.

Rules for choosing:

- Assign one Work Packet per cycle, copied as written unless the code shows a
  concrete problem with it, in which case route to `user` and name it.
- Prefer Phase 1 order. Packets that edit live role files (`.claude/`,
  `.codex/`, `.gemini/`, `AGENTS.md`, `docs/handoff/*_PROMPT.md`) are
  protected and go last within a phase; list those files under Files in
  scope when you do assign them.
- Implementers do not commit: the dispatcher commits in-scope files.
- `python -m pytest tests -q` takes about 30 seconds and is a fine
  Verification command.
