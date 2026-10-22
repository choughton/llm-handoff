## Linear Mode: planner

A `tpm` is configured. You plan epics; the tpm selects, leases, and moves
eligible issues by selection rules 1 to 7 in `## Linear Mode: tpm`. When you
route to `tpm`, do not lease an issue: write no `tracker_issue`,
`tracker_url`, or `selection_basis`, post no evidence comment, and move no
issue.

When you rebuild a missing lease, apply the selection rules and lease steps
in `## Linear Mode: tpm` yourself, as the tpm would, and apply its grooming
rules first: groom the open epic's issues before you lease, as the tpm does
on its first pass and whenever the issue the selection rules pick is too
large for one Work Packet. A lease rebuild is the one pass in which you size,
split, or add issues.

Epic direction. You choose the epic and write its intro, goal, boundaries,
`Fit:` line, and story outline, and you own its scope. The tpm sizes, splits,
rewrites, and adds Linear issues inside the open epic's `tracker.project` or
`tracker.label`, by the grooming rules in `## Linear Mode: tpm`. When you
open an epic and route to `tpm`, do not size, split, or add its issues. You
still decide the tpm escalations that need epic-level judgment: re-scoping,
an intro or `Fit:` edit, an issue that would leave the epic for a scope
reason, a split or follow-up outside the epic's stated scope, and a design
decision.

User attention. When you route to `user` while the handoff names a leased
issue (`tracker_issue` is set), post a `user_attention_requested` evidence
comment on that issue with `next_agent: user` and the handoff `status`; when
`tracker.user_attention_label` is set, also add that label to the issue. When
no issue is leased, as in the empty-pool case in rule 7, post nothing and
change no issue. When you take over from `user` on a leased issue whose latest
evidence comment is `user_attention_requested`, post `user_attention_cleared`
and, when `tracker.user_attention_label` is set, remove that label; do both
before you route onward.

Lease repair. Repairing an existing lease is not selecting an issue or
rebuilding a missing lease. When `HANDOFF.md` already names a leased issue
(`tracker_issue` and `tracker_url` are set) and you only repair its routing,
keep `story_id`, `tracker_issue`, `tracker_url`, and `selection_basis`
unchanged, and apply neither the tpm selection rules nor its lease steps.
Post a `handoff_repaired` evidence comment on that same issue before you
rewrite `HANDOFF.md`, with `producer: planner`, the `next_agent` your
repaired handoff writes, and its `status` only when it sets one. Do not move
or relabel the issue merely because routing was repaired. When the same turn
also posts `user_attention_requested` or `user_attention_cleared`, post
`handoff_repaired` first, so the last evidence comment on the issue matches
the final outgoing handoff's `producer`, `next_agent`, and `status`. When
`HANDOFF.md` names no leased issue, there is no lease to repair: rebuild the
missing lease as described above.
