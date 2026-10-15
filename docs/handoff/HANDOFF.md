---
next_agent: backend
reason: "Dispatch W1.2 to backend: harden the supervised child so it imports llm_handoff from the supervisor's own installation, even when the target repo contains an llm_handoff/ directory (F15)."
epic_id: SELFHOST-P1
story_id: W1.2
story_title: Isolate the supervised child from the target's working directory
status:
producer: planner
---

# Handoff: Isolate the supervised child from the target's working directory

## Previously Closed

- W0.1 "Decouple the live handoff from template parity" is verified at
  `d74a3e0e85b6b27690baf369b0d85fd67995c4f4` (close_type: story).
- W1.1 "Resolve the Codex output schema from the package" is verified at
  `20bb114ee061c5a7457d3eb6d7a8061992a27692` (close_type: story). Both
  auditor reviewers (quality and security) returned PASS; the two sub-threshold
  findings (`tests/ported/test_agents.py:1552` `Path.cwd()` join is a no-op,
  and the package-data note for a future `pyproject.toml`) are not blockers
  and are candidates for later cleanup / packaging packets, not for W1.2.

## Work Packet

- **Objective:** The supervised dispatch child imports `llm_handoff` from the
  supervisor's own installation, not from a copy of `llm_handoff/` that
  happens to live at the target `repo_root`. Concretely, when the supervisor
  spawns its child with `cwd=repo_root` and that `repo_root` contains any
  `llm_handoff/__init__.py` (for example, when the target repository is this
  repo or a fork of it), the child must still import the installed
  `llm_handoff` package. Launch behavior and exit codes must stay otherwise
  unchanged.
- **Files in scope:** `llm_handoff/supervisor.py`,
  `tests/test_supervisor.py`, `CHANGELOG.md`
- **Files out of bounds:** `llm_handoff/orchestrator.py`,
  `llm_handoff/__main__.py`, `llm_handoff/config.py`,
  `llm_handoff/agent_process.py`, `llm_handoff/agent_providers/`,
  `llm_handoff/schemas/`, `examples/`, `.codex/`, `.claude/`, `.gemini/`,
  `AGENTS.md`, `docs/handoff/` (except `docs/handoff/HANDOFF.md` for the
  handback itself)
- **Context:** F15 in `docs/proposals/linear-onboarding-gap-analysis.md`.
  Today `_run_dispatch_child` (`llm_handoff/supervisor.py:595-602`) builds the
  child command as `[sys.executable, "-m", "llm_handoff",
  *_child_dispatch_options(config), *config.dispatch_args]` and launches it
  with `cwd=config.repo_root` (`supervisor.py:606-608`). Python's `-m` prepends
  the current working directory to `sys.path`, so when the target repository
  is a checkout of llm-handoff itself the child imports the target's
  `llm_handoff/` instead of the installation that owns the supervisor. The
  supported Python floor is 3.11 (`INSTALL.md:12`), so `-P` (which blocks the
  prepending of the script directory / cwd to `sys.path`) is available.
- **Instructions:**
  1. Change `_run_dispatch_child` in `llm_handoff/supervisor.py` to insert
     `-P` immediately after `sys.executable` in the child command (before
     `-m` and the module name). Do not remove `cwd=config.repo_root`; the
     child still needs to run against the target repository, only its
     `sys.path` is being tightened.
  2. If the supervisor itself was loaded from a source checkout rather than
     from an installed package (detect via the location of `llm_handoff`'s
     `__file__` / `__spec__`), prepend that package's parent directory to
     `PYTHONPATH` in the child's environment only, so `-P` does not strip the
     ability of the child to find the supervisor's own package. Do not mutate
     `os.environ`; build a child-specific env dict. Model-vendor API keys
     and the dispatcher's other environment pass-through behavior must stay
     unchanged.
  3. Add tests in `tests/test_supervisor.py` that:
     - Build the child command (either by calling the helper that assembles
       it or by patching `subprocess.Popen` and inspecting the recorded
       argv) and assert the command contains `-P` positioned before `-m`.
     - Set up a `tmp_path` that contains a decoy `llm_handoff/__init__.py`
       (whose body would raise on import if executed), run the child in a
       controlled way (for example by patching `subprocess.Popen` and
       asserting the computed command plus env, or by using
       `subprocess.run` with `-c "import llm_handoff; print(llm_handoff.__file__)"`
       and `cwd=tmp_path`), and assert the resolved `llm_handoff.__file__`
       is NOT under `tmp_path`.
     - Confirm that when the supervisor is imported from a source checkout,
       the child env's `PYTHONPATH` contains the supervisor's package parent
       directory; when it is imported from an installed location, no
       `PYTHONPATH` injection is required (the test may simply assert the
       value is absent or unchanged).
  4. Add a one-line entry under `## Unreleased` in `CHANGELOG.md` describing
     the isolation fix. ASCII only.
  5. Do not alter `_child_dispatch_options` beyond what the above requires,
     and do not change the public shape of `SupervisorConfig`.
- **Verification command:** `python -m pytest tests/test_supervisor.py -q`
- **Expected next route:** auditor

## Next Step

- **backend:** Implement the `-P` isolation plus the supervisor tests, run the
  Verification command, and hand back with `## Verification Evidence` and
  `next_agent: auditor`. Do not run `git commit`; the dispatcher commits
  in-scope files in commit mode.

## Rules For This Run

- Local commits only; the dispatcher owns commits. Never run `git push`.
- Story-level close only; do not use `close_type: epic`.
- Keep changes within Files in scope. If you discover that the fix must
  touch an out-of-bounds file (for example `llm_handoff/__main__.py` or
  `llm_handoff/orchestrator.py`), stop and route to `planner` with the
  specific path and reason.
- Planning notes for later phases live in `docs/proposals/`, which is
  untracked on purpose. Read them if useful; never stage or commit them.
