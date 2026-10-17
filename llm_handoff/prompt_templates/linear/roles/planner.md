## Linear Mode: planner

- Read the issue pool: issues in `tracker.team`, filtered by
  `tracker.project` or `tracker.label`, in `tracker.eligible_state`.
- When you assign an issue, write the lease fields in `HANDOFF.md`:
  `story_id` and `tracker_issue` set to the issue identifier, `tracker_url`
  set to the issue URL, and `selection_basis`.
- Post a `lease_opened` evidence comment on the issue.
- Move the issue to `tracker.in_progress_state`.
- Then rewrite `HANDOFF.md`.
