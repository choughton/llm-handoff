## Linear Mode: finalizer

- You may read the leased issue named by `tracker_issue` and its comments.
- Your one Linear write is the `epic_closed` evidence comment below. Change
  no issue state, labels, or fields, select no new issue, and post no other
  comment.

Epic close. Post an `epic_closed` evidence comment only when the auditor
approved the epic close (the incoming handoff routes to you with
`close_type: epic`) and the incoming handoff names a leased issue in
`tracker_issue` and `tracker_url`. Post it on that issue, the last story's
issue, and set the comment's `issue` and `tracker_url` to those incoming
values. Set its `producer` to `finalizer` and its `next_agent` and `status`
to the values your outgoing `HANDOFF.md` writes. Post the comment after the
project state update and before you rewrite `HANDOFF.md`. In that rewrite,
keep `tracker_issue` and `tracker_url` unchanged so the evidence check can
compare them.

Missing lease. Never guess an issue. When the incoming handoff has no
`tracker_issue` or `tracker_url`, or the connector is unavailable, or a
Linear read or the comment post fails, post nothing and write
`status: blocked_missing_context` with `next_agent: user` and one specific
question for the user.
