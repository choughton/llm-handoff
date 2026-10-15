---
next_agent: planner
reason: "Phase 0 self-hosting bootstrap: scope W0.1 for the backend role."
epic_id: SELFHOST-P0
story_id: W0.1
story_title: Decouple the live handoff from template parity
status:
producer: user
---

# Handoff: Phase 0 self-hosting bootstrap

## Current State

llm-handoff is now developed with its own dispatch loop. A pinned dispatcher
(a separate clone, detached at `4e6c352`) runs against this checkout on branch
`selfhost/phase-1`, with every role on Claude, the work-claim gate on, and the
security reviewer on.

This first assignment proves that arrangement. While this file holds live
dispatch state, `tests/test_reference_workflow.py` fails, because it requires
the root `docs/handoff/HANDOFF.md` to match the template byte for byte. W0.1
fixes that.

## Requested Assignment

Write a planner assignment that routes W0.1 to `backend` with this Work
Packet. Copy it as written unless you find a concrete problem with it; if you
do, route to `user` and name the problem.

- **Objective:** `python -m pytest tests -q` passes when the root
  `docs/handoff/HANDOFF.md` holds live dispatch state, while the template
  starter handoff is still checked.
- **Files in scope:** `tests/test_reference_workflow.py`
- **Files out of bounds:** `llm_handoff/`, `examples/reference-workflow/`,
  `.claude/`, `.codex/`, `.gemini/`, `AGENTS.md`, `docs/handoff/` (except
  `docs/handoff/HANDOFF.md` for the handback itself)
- **Context:** Remove `"docs/handoff/HANDOFF.md"` from
  `COPIED_TEMPLATE_FILES` (`tests/test_reference_workflow.py:43`). Keep
  `test_reference_workflow_starter_handoff_routes_to_planner`
  (`tests/test_reference_workflow.py:115`), which still covers the template
  copy. No production code changes. Every other root file in
  `COPIED_TEMPLATE_FILES` must still match its template copy.
- **Verification command:** `python -m pytest tests -q`
- **Expected next route:** auditor

## Rules For This Run

- Local commits only. Never run `git push`.
- Story-level close only; do not use `close_type: epic`.
- Planning notes for later phases live in `docs/proposals/`, which is
  untracked on purpose. Read them if useful; never stage or commit them.
