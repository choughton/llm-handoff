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

Transitions. On a pass, post an `audit_passed` evidence comment with
`state_from` set to the value of `tracker.in_progress_state` and `state_to`
set to the value of `tracker.success_state`, then move the leased issue to
`tracker.success_state`. The dispatcher routes to you only after the
work-claim gate passed, so the move to `tracker.success_state` happens only
on a pass. On a fail, post an `audit_failed` evidence comment that lists the
findings and leave the issue in `tracker.in_progress_state`; never move it to
`tracker.success_state`. Move only the issue named by `tracker_issue`, and
never change any other issue. Post the evidence comment before the state
change, and do both before you rewrite `HANDOFF.md`.

User attention. When you route to `user` while the handoff names a leased
issue (`tracker_issue` is set), post a `user_attention_requested` evidence
comment on that issue with `next_agent: user` and the handoff `status`; when
`tracker.user_attention_label` is set, also add that label to the issue. When
`tracker.blocked_state` is set, also move the issue to `tracker.blocked_state`
and record `state_from` and `state_to` in that comment. When your turn starts
on a leased issue whose latest evidence comment is `user_attention_requested`,
post `user_attention_cleared` and, when `tracker.user_attention_label` is set,
remove that label; do both before you post `audit_passed` or `audit_failed`.
