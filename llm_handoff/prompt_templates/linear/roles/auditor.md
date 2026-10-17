## Linear Mode: auditor

- Read the leased issue named by `tracker_issue`, and its comments,
  including the implementer's `implementation_handoff` evidence comment.
- After your review, post an `audit_passed` or `audit_failed` evidence
  comment on the leased issue. For `audit_failed`, list the findings in the
  comment.
- Set `commit_sha` to the concrete commit you reviewed, the `scope_sha` in
  `HANDOFF.md`, even when the implementer's comment says
  `pending-dispatcher-commit`.
- Then rewrite `HANDOFF.md`, keeping `tracker_issue`, `tracker_url`, and
  `selection_basis` unchanged.
