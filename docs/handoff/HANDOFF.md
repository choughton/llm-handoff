---
next_agent: backend
reason: "Dispatch W0.1 to backend: decouple the live handoff from template parity so pytest passes while the root HANDOFF.md holds live dispatch state."
epic_id: SELFHOST-P0
story_id: W0.1
story_title: Decouple the live handoff from template parity
status:
producer: planner
---

# Handoff: Decouple the live handoff from template parity

## Work Packet

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

## Next Step

- **backend:** Make the single-line edit to `COPIED_TEMPLATE_FILES` described
  above, run `python -m pytest tests -q`, commit locally, then hand back with
  `## Verification Evidence` and `next_agent: auditor`.

## Rules For This Run

- Local commits only. Never run `git push`.
- Story-level close only; do not use `close_type: epic`.
- Planning notes for later phases live in `docs/proposals/`, which is
  untracked on purpose. Read them if useful; never stage or commit them.
