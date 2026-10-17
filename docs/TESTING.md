# Testing

The test suite should make dispatcher behavior explicit. Each test should cover
a concrete routing, validation, config, or provider-command construction
contract.

## Current State

The current repository is a pre-release scaffold. The full dispatcher test
suite now runs as one public gate, including the ported migration coverage and
the focused public-default tests.

Current full-suite result on Windows:

```text
1344 passed, 18 skipped
```

One test, `test_timed_out_cmd_shim_child_is_killed_with_its_tree` in
`tests/test_process_tree.py`, launches a real Windows `.cmd` shim process tree
and is skipped on other platforms, where the result is `724 passed, 1 skipped`.
Two tests in `tests/test_npm_shim_launch.py` launch a fake npm `.cmd` shim
through `node` and are skipped off Windows or when `node` is not on `PATH`.

The `tests/ported/` directory is still named as migration history, but it is
part of the release gate. Individual tests can be moved or renamed later as the
suite is reorganized.

## Running Tests

```bash
python -m pytest tests -q
```

`pytest.ini` runs the suite on 8 `pytest-xdist` workers, which keeps a full
run well under a minute. Run serially with `-n 0` when debugging a single test
or reading its output with `-s`.

Tests must never launch a real provider CLI (`claude`, `codex`, `gemini`,
`agy`): that is slow, spends the developer's tokens, and depends on their
sign-in. An autouse fixture in `tests/conftest.py` fails any test that tries.
Stub the agent call instead (for example `orchestrator.invoke_support_role`),
or put a fake executable under the test's temporary directory.

## Integration Tests

`tests/integration/` checks that the installed command works on repositories
unrelated to `llm-handoff`. Each test builds the wheel and installs it into a
temporary directory, offline (`--no-index`, `--no-build-isolation`, so
`hatchling` from `requirements-dev.txt` must be installed). It then runs
`python -P -m llm_handoff` from a directory outside the checkout against a
throwaway Node repository (existing `AGENTS.md` and `.claude/settings.json`)
and a throwaway Go repository with one commit.

Each repository is run twice, once with only a fake `claude` and once with
only a fake `codex` on a controlled `PATH`. The fakes print a version and
exit, so no real provider CLI or network is used. The tests check that
`init` writes the single-provider mapping, keeps existing content, and is
idempotent; that `doctor` and `--dry-run` pass; and that nothing outside
`init`'s writes changes.

The tests carry the `integration` marker and also run in the full gate. Run
them alone with:

```bash
python -m pytest tests/integration -q -m integration
```

## Local Hooks

The repo includes a light pre-commit configuration for whitespace, YAML/TOML
syntax, large-file checks, and Ruff. Install and run it with:

```bash
python -m pip install -r requirements-dev.txt
pre-commit install
pre-commit run --all-files
```

The hooks are intentionally fast. They do not run pytest; use the pytest command
above for the full gate.

Run focused files during extraction:

```bash
python -m pytest tests/test_public_defaults.py -q
python -m pytest tests/test_config.py -q
python -m pytest tests/test_generic_router.py -q
python -m pytest tests/test_cli.py -q
python -m pytest tests/test_init_workflow.py -q
python -m pytest tests/test_handoff_normalizer.py -q
python -m pytest tests/ported/test_handoff_docs.py -q
```

## Test Categories

Expected test areas:

- CLI help and config loading.
- Target-repo reference workflow initialization.
- Repo-root detection.
- Handoff frontmatter parsing.
- Generic role routing.
- Rejection of project-specific aliases.
- Validator verdict parsing.
- Provider command construction.
- Lockfile behavior.
- Semantic SHA validation.
- Prompt-template loading.

## Test Principles

- Prefer deterministic unit tests for routing and validation.
- Mock provider CLIs; do not call networked tools in unit tests.
- Keep fixtures readable.
- Test failure modes by name.
- Record expected pre-release failures clearly in commit messages or PR notes.

## Before Release

Before the first public tag, the suite should pass without expected failures:

```bash
python -m pytest tests -q
```

The release checklist should also include a smoke test against a disposable
target repo using the reference workflow template.
