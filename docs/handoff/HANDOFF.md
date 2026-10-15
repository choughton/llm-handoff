---
next_agent: backend
reason: "Dispatch W1.1 to backend: resolve the Codex --output-schema path from the installed llm_handoff package instead of repo_root so Codex launches work in target repos that do not ship an llm_handoff/ directory."
epic_id: SELFHOST-P0
story_id: W1.1
story_title: Resolve the Codex output schema from the package
status:
producer: planner
---

# Handoff: Resolve the Codex output schema from the package

## Work Packet

- **Objective:** Codex launches pass an `--output-schema` path inside the
  installed `llm_handoff` package, regardless of `repo_root`. The remaining
  Codex artifact paths (`logs/dispatch/codex/last-message.json`, the session
  state file, the output directory) must still resolve under `repo_root`.
- **Files in scope:** `llm_handoff/config.py`,
  `llm_handoff/agent_providers/codex.py`, `tests/test_launch_flags.py`,
  `CHANGELOG.md`
- **Files out of bounds:** `llm_handoff/schemas/`, `examples/`, `.codex/`,
  `.claude/`, `.gemini/`, `AGENTS.md`, `docs/handoff/` (except
  `docs/handoff/HANDOFF.md` for the handback itself)
- **Context:** F1 in `docs/proposals/linear-onboarding-gap-analysis.md`. Today
  `llm_handoff/config.py:105-107` declares
  `CODEX_OUTPUT_SCHEMA_PATH = Path("llm_handoff") / "schemas" /
  "codex_final_response.schema.json"`, and
  `llm_handoff/agent_providers/codex.py:395` resolves it with
  `(repo_root / config.CODEX_OUTPUT_SCHEMA_PATH).resolve()`. In a target
  repository without an `llm_handoff/` directory, that path does not exist and
  Codex launches fail. Resolve the schema from the installed package instead,
  for example with `importlib.resources.files("llm_handoff") / "schemas" /
  "codex_final_response.schema.json"` or a `Path(__file__)`-relative path.
  Only the schema path moves; `CODEX_OUTPUT_DIRECTORY`,
  `CODEX_OUTPUT_LAST_MESSAGE_PATH`, and `CODEX_SESSION_STATE_PATH` must still
  resolve under `repo_root` in `_codex_artifact_paths`. Keep the Codex command
  shape unchanged (still a single `--output-schema <path>` argument).
- **Instructions:**
  1. Change the schema path to resolve from the package (not `repo_root`) in
     `llm_handoff/config.py` and/or `llm_handoff/agent_providers/codex.py`.
     Keep the public name usable where it is already referenced
     (`tests/ported/test_agents.py:1552` reads `config.CODEX_OUTPUT_SCHEMA_PATH`;
     update that call site only if the type/shape changes, and keep the test's
     assertion that the Codex command names the schema path).
  2. Add a test in `tests/test_launch_flags.py` that builds the Codex launch
     command for a `repo_root` with no `llm_handoff/` directory (for example a
     fresh `tmp_path`) and asserts the resolved `--output-schema` path exists
     on disk and lives inside the installed `llm_handoff` package tree, not
     under `repo_root`.
  3. Add a one-line entry under `## Unreleased` in `CHANGELOG.md` describing
     the fix.
  4. Do not touch `llm_handoff/schemas/codex_final_response.schema.json` or
     any file under `examples/`.
- **Verification command:** `python -m pytest tests/test_launch_flags.py tests/ported/test_agents.py -q`
- **Expected next route:** auditor

## Next Step

- **backend:** Implement the schema-path change plus the new launch-flags
  test, run the Verification command, and hand back with
  `## Verification Evidence` and `next_agent: auditor`. Do not run `git
  commit`; the dispatcher commits in-scope files in commit mode.

## Rules For This Run

- Local commits only; the dispatcher owns commits. Never run `git push`.
- Story-level close only; do not use `close_type: epic`.
- Keep changes within Files in scope. If you discover that the fix must
  touch an out-of-bounds file, stop and route to `planner` with the specific
  path and reason.
- Planning notes for later phases live in `docs/proposals/`, which is
  untracked on purpose. Read them if useful; never stage or commit them.
