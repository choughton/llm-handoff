# Project State

This file is the durable state pointer for a repository using `llm-handoff`.
Keep it short. Detailed implementation notes belong in commits, handoffs, or
project docs.

## Current Status

- **Active Epic:** None - awaiting planner scoping.
- **Current Blocker:** None.
- **Active Branch:** main.

## Open Followups

- None.

## Notes

- `docs/handoff/HANDOFF.md` is the live state file.
- `PROJECT_STATE.md` is the durable status file. It holds current work only:
  the finalizer moves each closed epic and its ledger line to
  `COMPLETED_WORK.md`.
- Git history is the durable record of completed work.
