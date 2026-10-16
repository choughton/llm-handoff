---
next_agent: planner
reason: "SELFHOST-P1 epic closed at 67066c0; planner should select the next bounded assignment."
epic_id: "SELFHOST-P1"
story_id: "W1.3"
story_title: "Remove template references to llm-handoff's own docs (live role files; schedule last in phase)"
status: verified_pass
scope_sha: 67066c0807e020a63be2e9effbfd53ac26577b19
close_type: story
prior_sha: "ec83e1a"
producer: finalizer
---

# Finalizer Report - SELFHOST-P1 epic close

- Epic SELFHOST-P1 closed at 67066c0807e020a63be2e9effbfd53ac26577b19 (audit APPROVED, 0 blocking findings).
- No repository-level PROJECT_STATE.md exists and AGENTS.md does not say this repo uses one, so project state was left unchanged. The durable record is git first-parent history on main (W1.3 merge ec83e1a, PR #46 merge 67066c0).
- PR #46 is included in the epic close SHA but was not part of the epic's stories (landed outside the loop; additive, no reverts).
- Open low findings for the planner: publish.py:342 (merge-step pause becomes push-step pause if resumed sync conflicts again) and publish.py:616 (resumed publish can push and auto-merge unaudited commits).

The audit report below is retained for reference.

# Audit Report - W1.3 (Remove template references to llm-handoff's own docs (live role files; schedule last in phase))

**Agent:** auditor
**Audit mode:** read-only fan-out (quality: claude, security: claude) on `ec83e1ad6a3f5e3e66f3562b6563091f84c14916..67066c0807e020a63be2e9effbfd53ac26577b19`; the dispatcher wrote this handoff from the reviewers' structured verdicts.

## Verdict

APPROVED

## Merge Decision

- Quality reviewer verdict: pass
- Security reviewer verdict: pass
- Block severity threshold: high
- Blocking findings: 0
- Sub-threshold findings: 6

## Quality Review

SELFHOST-P1 epic close at 67066c0 approved: all four Work Packet acceptance checks hold and PR #46 adds to the SELFHOST-P1 work without reverting any of it

### Audit Report - SELFHOST-P1 epic close at 67066c0

#### Verdict
APPROVED WITH NITS

#### Phase 1: Spec Compliance (Work Packet acceptance checks)

1. **First-parent story merges: PASS.** `git log --oneline --first-parent main` shows the W1.3 merge `ec83e1a` (#45) and every story merge the planner listed: W1.4 #13 (f5e15a8), W1.5 #25 (c277547), W1.6 #28 (af82f05), W1.7c #29 (7677bb3), W1.7d #30 (e99b185), W1.8 #34 (556e4eb), W1.10 #35 (ec30aa5), W1.11 #36 (62647a4), W1.12 #37 (e4ac373), W1.12b #38 (bdbc4a3), W1.13 #39 (684a123), W1.7b #40 (1928477), W1.9 #42 (7d73340), W1.7e #44 (d37fdca).
2. **`python -m pytest tests -q` at 67066c0: PASS.** Exit 0, with 1358 passed, 18 skipped and 1 third-party DeprecationWarning. I ran it because HANDOFF.md has no `## Dispatcher Verification` section. HEAD is 67066c0807e020a63be2e9effbfd53ac26577b19.
3. **Template grep: PASS.** Grepping `examples/reference-workflow/` for `ARCHITECTURE.md|CONFIGURATION.md` matches only `dispatch_config.yaml` lines 15, 27 and 41. Each of those lines contains `llm-handoff's CONFIGURATION.md`.
4. **PR #46 does not undo SELFHOST-P1 work: PASS.** The `ec83e1a..67066c0` diff touches `.gitattributes`, `CHANGELOG.md`, `CONFIGURATION.md`, `llm_handoff/orchestrator.py`, `llm_handoff/publish.py`, `tests/test_publish.py` and `tests/test_work_claim.py`. It removes only 14 lines in total:
   - 2 in `CONFIGURATION.md`: the failure-mode sentence was reworded to add fetch and the pause/retry exceptions.
   - 9 in `publish.py`: a module docstring line, the `_merge_pull_request` docstring and call, and the `_pause` signature.
   - 3 in `tests/test_publish.py`: an inlined `update-ref` call was refactored, and the push-failure test was renamed to a fetch-failure test. A new copy of the push-failure test was added alongside it.

   It does not touch any template, role prompt or SELFHOST-P1 surface. The existing W1.3 CHANGELOG bullet is still there.

The Work Packet's files in scope are limited to `docs/handoff/HANDOFF.md`. That limit applies to the audit pass, not to PR #46, which the planner disclosed as a fix made outside the loop.

#### Phase 2: Code Quality (PR #46, informational)

- The base sync (`publish.py` `_sync_with_base`) fetches with an explicit refspec and merges with `--no-edit --no-verify`. If the merge fails, it aborts only when `MERGE_HEAD` exists, saves `paused_publish`, and pauses with `keep_state=True`. This is sound.
- The not-mergeable retry is bounded to one attempt (`_merge_pull_request`). It re-reads the state before syncing and saves `paused_publish="merge"` if the second merge fails.
- The resume path in `orchestrator.py:340-355` is gated on `mode == "pr"` and `not dry_run`. Parametrized tests in `tests/test_work_claim.py` cover it.
- An operator merge that has not been reviewed is not auto-merged when reviewers are configured (`publish.py:399-409`). Tests cover this with and without a review gate.
- One low-severity edge case is listed under Findings. It does not block the epic close.

#### Checks Run
- `python -m pytest tests -q`: PASS (exit 0, 1358 passed, 18 skipped)
- `git log --oneline --first-parent main`: PASS
- Grep of `examples/reference-workflow/`: PASS
- `git diff` / `--numstat` of `ec83e1a..67066c0`: PASS (no reverts)

#### Verification Evidence

- **Commands run:** `git rev-parse HEAD`; `git log --oneline --first-parent main -30`; `git log --oneline --first-parent main --grep="#13 "`; `python -m pytest tests -q`; `git diff --stat|--numstat|-- <paths>` over `ec83e1a..67066c0`
- **Output summary:** all exit 0; pytest 1358 passed, 18 skipped
- **Commit SHA verified:** 67066c0807e020a63be2e9effbfd53ac26577b19
- **Files changed or reviewed:** .gitattributes, CHANGELOG.md, CONFIGURATION.md, llm_handoff/orchestrator.py, llm_handoff/publish.py, tests/test_publish.py, tests/test_work_claim.py, examples/reference-workflow/dispatch_config.yaml, docs/handoff/HANDOFF.md
- **Unresolved concerns:** none

#### Next Step
- **finalizer:** Close epic SELFHOST-P1 at 67066c0807e020a63be2e9effbfd53ac26577b19 and update PROJECT_STATE.md.

### Quality Findings

- [low] llm_handoff/publish.py:342 - A merge-step pause becomes a push-step pause if the resumed sync conflicts again
  - Description: When resuming a publish that paused at the merge step, publish_story records resumed_at_merge, clears paused_publish (lines 342-345), then calls _sync_with_base with step="push" (line 346). If the base branch moved again and conflicts again, the state is saved as paused_publish="push", so the merge-step origin is lost. On the next resume, with wait_for_reviewers [codex], the dispatcher pushes to the already-open PR and waits for a Codex review that will not come for a later push. The run ends in a reviewer wait timeout or pause instead of the intended 'left open for a person' pause at lines 399-409.
  - Recommendation: Pass step="merge" to _sync_with_base when resumed_at_merge is true, or otherwise keep the merge marker when the resumed sync pauses again. Add a test for two conflicts in a row.
- [info] docs/handoff/HANDOFF.md:27 - PR #46 landed outside the loop under the epic close SHA
  - Description: 67066c0 includes a dispatcher change (PR #46) that no SELFHOST-P1 story covered. The planner disclosed this. The change adds to existing code without reverting anything, and the full suite passes.
  - Recommendation: Have the finalizer note in PROJECT_STATE.md that PR #46 is included in the epic close SHA but was not part of the epic's stories.

## Security Review

The base-branch sync and paused-publish resume changes have no exploitable injection, secret, or deserialization flaws; the one real issue is that resume can push and merge work nobody audited when no reviewers are configured.

## Security Notes

- [low] llm_handoff/publish.py:616 - Resumed publish pushes and can auto-merge commits that were never audited
  - Description: resume_paused_publish (called at startup from orchestrator.py:340) calls publish_story with no new work-claim or audit gate. The only checks are that HANDOFF.md frontmatter says verified_pass (publish.py:287), that story.json has paused_publish set, and that the story branch is checked out (publish.py:637). Whatever is on the story branch at that point gets pushed: the operator's conflict-resolution merge plus any other commit added while the loop was paused. With auto_merge: true and no wait_for_reviewers, it is then merged into the base branch. The resumed_at_merge pause (publish.py:399) only applies when reviewers are configured. Pause-at-push resumes always go through the normal publish path, and for a new pull request the Codex review covers that. CONFIGURATION.md documents this, and it needs local write access to the checkout and to logs/dispatch/publish/story.json, so it is a hardening gap, not a remote exploit.
  - Recommendation: When saving the pause, record the story HEAD. On resume, check that the only new commits since then are merge commits whose parents include the recorded HEAD and the remote base tip. Otherwise pause, or rerun the work-claim and audit gates. You could also pause after the push whenever auto_merge is on and nobody has reviewed the resumed merge, not just when reviewers are configured.
- [info] llm_handoff/publish.py:487 - Retry after a 'not mergeable' error merges a new base-merge commit without re-review
  - Description: When gh pr merge output contains 'not mergeable', _merge_pull_request merges the base branch into the story branch, pushes, and retries gh pr merge right away. The new merge commit is never reviewed, even with wait_for_reviewers set. The merge is a clean, machine-made merge of already-merged base content, so the risk is low, but it is inconsistent with the resumed_at_merge rule that pauses on unreviewed merges. The trigger is a substring match on gh output, which is brittle but cannot be exploited.
  - Recommendation: Optionally re-check review threads and required checks (wait_for_checks) after the retry push before merging, and document that the retried merge commit is unreviewed.
- [info] .gitattributes:9 - merge=union on CHANGELOG.md can silently combine content
  - Description: A union merge keeps lines from both sides without reporting a conflict. For CHANGELOG.md that is an integrity issue, not a security one, and CONFIGURATION.md warns about it and does not add it to target repositories by default.
  - Recommendation: Keep union merges limited to append-only files like the changelog, as documented.
- [info] llm_handoff/publish.py:555 - Git arguments built from state and config values, with no shell
  - Description: _sync_with_base builds fetch and merge arguments from state.base_branch (read from the local story.json) and remote (from config). They are passed as an argument list with no shell. The base branch sits inside a '+refs/heads/...' refspec, so it cannot be read as an option. The merge argument 'remote/base' could only be read as an option if the configured remote started with '-'. --no-verify skips local hooks on the dispatcher's own merge commit, which is the intended behavior. There is no shell or command injection.
  - Recommendation: Optionally check that the remote and branch names do not start with '-' (or use git check-ref-format) when loading config and state, as defense in depth.

## Verification Evidence

- **Commands run:** git diff --stat ec83e1ad6a3f5e3e66f3562b6563091f84c14916 67066c0807e020a63be2e9effbfd53ac26577b19; git log --oneline --first-parent main -30; git rev-parse HEAD; git log --oneline --first-parent main --grep="#13 "; python -m pytest tests -q; git diff ec83e1ad6a3f5e3e66f3562b6563091f84c14916 67066c0807e020a63be2e9effbfd53ac26577b19 -- llm_handoff CONFIGURATION.md .gitattributes CHANGELOG.md tests/test_work_claim.py; git diff --numstat ec83e1ad6a3f5e3e66f3562b6563091f84c14916 67066c0807e020a63be2e9effbfd53ac26577b19; git diff -U0 ec83e1ad6a3f5e3e66f3562b6563091f84c14916 67066c0807e020a63be2e9effbfd53ac26577b19 -- tests/test_publish.py --diff-filter=M -G"^-"; security reviewer: claude --permission-mode dontAsk --tools Read,Grep,Glob,Bash --strict-mcp-config --allowedTools Read Grep Glob Bash(git diff *) Bash(git show *) Bash(git log *) --disallowedTools Edit Write MultiEdit NotebookEdit WebFetch WebSearch --append-system-prompt <long-arg> --model claude-opus-5-5 --output-format json --json-schema <long-arg> --no-session-persistence -p <long-arg>
- **Output summary:** quality reviewer (claude) exit 0, verdict pass, 2 finding(s), 175.3s; security reviewer (claude) exit 0, verdict pass, 4 finding(s), 41.1s; git diff --stat: exit 0, 7 files, +645/-14. git log first-parent -30: exit 0, shows 67066c0 (#46), ec83e1a (#45) and #44, #42, #40, #39, #38, #37, #36, #35, #34, #30, #29, #28, #25. git rev-parse HEAD: exit 0, 67066c0807e020a63be2e9effbfd53ac26577b19. git log --grep #13: exit 0, f5e15a8 (W1.4 #13). pytest tests -q: exit 0, 1358 passed, 18 skipped, 1 warning in 81.50s (run because HANDOFF.md has no Dispatcher Verification section). git diff code paths: exit 0, reviewed. git diff --numstat: exit 0, deletions only in CONFIGURATION.md (2), publish.py (9), test_publish.py (3). git diff -U0 test_publish.py: exit 0, the 3 removed lines are a refactor of the fake gh update-ref call and one test rename; the original push-failure test is kept.
- **Commit SHA verified:** 67066c0807e020a63be2e9effbfd53ac26577b19
- **Files changed or reviewed:** .gitattributes, CHANGELOG.md, CONFIGURATION.md, llm_handoff/orchestrator.py, llm_handoff/publish.py, tests/test_publish.py, tests/test_work_claim.py
- **Unresolved concerns:** none

## Next Step

- **planner:** SELFHOST-P1 is closed; select the next bounded assignment.
