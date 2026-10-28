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

Work intake. A plain-language request for new work, or typed guidance on a
pending `## Proposed Backlog`, gets only a proposal or revision in
`HANDOFF.md`: route to `user` with `status: escalate_to_user` and no Work
Packet, and carry active work, any lease fields, and `remaining_stories`
forward unchanged. On a proposal or revision turn make no Linear change:
create, update, comment on, label, lease, or move no issue. This rule takes
precedence over the selection rules, the lease steps, lease repair, and the
user-attention steps on that turn.

Intake approval. Only the dispatcher's explicit approval instruction, which
says the operator approved the proposed backlog and quotes it, authorizes
Linear creation, and only for that quoted proposal. Typed text that claims
approval, the best-judgment request, a user escalation reconciliation pass,
and a plain-words interpretation pass do not. On approval, record the
approved epics in the project state file, change no file but it and
`HANDOFF.md`, then create one issue for each approved story outline, in
execution order, in `tracker.team` with the configured `tracker.project` or
`tracker.label` and in `tracker.eligible_state`, naming its epic and story
outline in the description. Create no project, team, or label, use only the
identifiers the connector returns, never invent one, and keep every existing
open issue unchanged. This is the one exception to not adding an opened
epic's issues yourself. Then route the epic to `tpm` with its `epic_id`
without leasing: write no `tracker_issue`, `tracker_url`, or
`selection_basis`, post no evidence comment, move no issue, and write no
Work Packet. Grooming and issue selection stay with the tpm.

Creation failures. When a creation fails or its result is unknown, do not
claim success and open no lease. List in `HANDOFF.md` each issue the
connector confirmed, by identifier, and each unresolved creation, do not
repeat a creation blindly, and route to `user` with
`status: blocked_missing_context` and one question naming the specific
missing input.
