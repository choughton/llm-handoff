## Linear Mode: planner

A `tpm` is configured. You plan epics; the tpm selects, leases, and moves
eligible issues by selection rules 1 to 7 in `## Linear Mode: tpm`. When you
route to `tpm`, do not lease an issue: write no `tracker_issue`,
`tracker_url`, or `selection_basis`, post no evidence comment, and move no
issue.

When you rebuild a missing lease, apply the selection rules and lease steps
in `## Linear Mode: tpm` yourself, as the tpm would.

User attention. When you route to `user` while the handoff names a leased
issue (`tracker_issue` is set), post a `user_attention_requested` evidence
comment on that issue with `next_agent: user` and the handoff `status`; when
`tracker.user_attention_label` is set, also add that label to the issue. When
no issue is leased, as in the empty-pool case in rule 7, post nothing and
change no issue. When you take over from `user` on a leased issue whose latest
evidence comment is `user_attention_requested`, post `user_attention_cleared`
and, when `tracker.user_attention_label` is set, remove that label; do both
before you route onward.
