# Repository Instructions

These instructions apply to the entire `llm-handoff` repository.

## Project Purpose

`llm-handoff` is a reference implementation of a file-based dispatch loop for
multi-CLI AI coding workflows. The core idea is:

- prompts are advisory;
- validators are authoritative;
- `HANDOFF.md` is the shared state file;
- Git commit SHAs are the durable record of completed work.

This repo is being extracted from a project-specific dispatcher. Keep public
code, docs, examples, and tests generic. Do not add project-specific product
requirements, private workflows, internal logs, or proprietary prompt text.

## Source Of Truth

Use these files as the repo-level source of truth once they exist:

- `README.md` for the public positioning and user workflow.
- `dispatch_config.example.yaml` for the supported configuration surface.
- `examples/reference-workflow/` for copyable protocol templates.
- `llm_handoff/` for the dispatcher module.
- `tests/` for the behavioral contract.

If a code path and the README disagree, update the narrower technical artifact
first, then adjust the README to match.

## Scope Boundaries

Keep the tool small and inspectable.

- Do not turn this into a general multi-agent framework.
- Do not add a web UI, service daemon, database, queue, or plugin marketplace.
- Do not abstract away Git. Git is part of the mechanism.
- Do not assume Crossfire-specific names, paths, or invariants.
- Do not auto-push by default. Local commits are acceptable; remote pushes must
  be explicit user-controlled behavior.

The public default workflow should be usable as a reference implementation even
when users choose to fork or adapt it.

## Naming

Use these public names consistently:

- Repository: `llm-handoff`
- Python module: `llm_handoff`
- Console command: `llm-handoff`
- Default handoff path: `docs/handoff/HANDOFF.md`
- Optional flat handoff path: `HANDOFF.md`
- Generic project state file: `PROJECT_STATE.md`

Avoid old internal names such as `llm_dev_team_dispatcher`, `crossfire_pe`,
`crossfire_frontend`, `crossfire_backend`, and `llm-crossfire-codex` except in
tests that explicitly verify those names are rejected or removed.

## Agent Role Vocabulary

Use generic role names in public docs, examples, and config:

- `planner`
- `backend`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider names such as Codex, Gemini, and Claude should appear only as adapter
or example mappings, not as required workflow roles.

## Editing Rules

- Prefer small, reviewable changes.
- Keep generated artifacts out of the repo.
- Preserve Apache-2.0 licensing unless the user explicitly changes the license.
- Write ASCII text unless a file already uses non-ASCII or the character is
  necessary.
- Avoid adding dependencies unless they are required for the dispatcher runtime
  or test suite.

## Testing

Use focused tests for every behavior change.

Before handing off implementation work, run the relevant tests. For broad
changes, run:

```powershell
python -m pytest tests -q
```

If tests cannot run because the extraction is incomplete, say that clearly and
list the blocking import, config, or dependency issue.

## Documentation Tone

Be direct and conservative.

Do not claim that this is production-ready for arbitrary repositories. The
correct positioning is:

> A reference implementation of a file-based handoff dispatcher for multi-CLI
> agent workflows.

Avoid hype terms and avoid implying self-healing. The dispatcher detects
failure modes, dispatches validators, and pauses. Humans or configured agents
perform recovery.
