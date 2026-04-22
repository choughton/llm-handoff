---
next_agent: planner
reason: "Initial public template. Replace this with the live routing reason."
epic_id: EXAMPLE-EPIC
story_id: EXAMPLE-EPIC-S1
story_title: Example story title
remaining_stories:
  - EXAMPLE-EPIC-S2
status:
bounce_count: 0
evidence_present:
scope_sha:
close_type:
prior_sha:
producer: user
---

# Handoff Template

This file is the shared state surface for the dispatcher. Agents overwrite it when they complete work, request review, escalate, or route the next step.

## Current State

Replace this section with the current assignment, handback, audit result, finalizer result, or escalation context.

## Routing Notes

- `next_agent` must be one of `planner`, `backend`, `frontend`, `auditor`, `validator`, `finalizer`, or `user`.
- `reason` must be quoted when it contains punctuation.
- `producer` must name the role that wrote this handoff.
- `status` should use the canonical enum when the handoff claims completion or blockage.
- Completion statuses require a `## Verification Evidence` block.
- `scope_sha` is required when `close_type` is set and must be a concrete 7-40 character Git SHA, not `HEAD`, a branch name, or a placeholder.
