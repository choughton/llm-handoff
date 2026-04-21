# Testing

The test suite should make dispatcher behavior explicit. Each test should cover
a concrete routing, validation, config, or provider-command construction
contract.

## Current State

The current repository is a pre-release scaffold. The full dispatcher test
suite now runs as one public gate, including the ported migration coverage and
the focused public-default tests.

Current full-suite result:

```text
310 passed
```

The `tests/ported/` directory is still named as migration history, but it is
part of the release gate. Individual tests can be moved or renamed later as the
suite is reorganized.

## Running Tests

```bash
python -m pytest tests -q
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
