## Linear Mode: validator

- Your Linear access is read-only. You may read the leased issue named by
  `tracker_issue` and its comments.
- Post no comments and make no issue updates.

Evidence check. Run this read-only check on every Linear-mode turn:

1. Find the latest evidence comment on the issue named by `tracker_issue`:
   the newest comment whose body is one fenced YAML block with the root key
   `llm_handoff_event`. Ignore every other comment.
2. Compare that comment with `HANDOFF.md`, field by field:
   - `issue` equals the handoff `tracker_issue`.
   - `tracker_url` equals the handoff `tracker_url`.
   - `producer` equals the handoff `producer`.
   - `next_agent` equals the handoff `next_agent`.
   - `status` equals the handoff `status` when the handoff sets one.
   - `commit_sha` equals the handoff `scope_sha`. The one exception is an
     `implementation_handoff` comment whose `commit_sha` is
     `pending-dispatcher-commit` when `commit_mode` is `dispatcher`.
   - The comment's `producer` is a role the evidence-comment contract allows
     to post that event.
   - For `implementation_handoff`, `audit_passed`, and `audit_failed`, the
     fields `commit_sha`, `commands_run`, `output_summary`, and
     `unresolved_concerns` are present.
3. Each of these also counts as a mismatch: the handoff has no
   `tracker_issue`, the issue has no evidence comment, or the latest evidence
   comment's `producer` differs from the handoff `producer`.
4. Write the result in the `HANDOFF.md` body under a
   `## Linear Evidence Check` heading, one line per mismatched field, naming
   both the comment value and the handoff value.
5. On any mismatch, write `status: blocked_missing_context` with
   `next_agent: user` and one specific question for the user. With no
   mismatch, state in that section that the check passed.
6. Never edit a comment or the issue to make them match. This check is
   read-only.
