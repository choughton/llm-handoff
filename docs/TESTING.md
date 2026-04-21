# Testing

The test suite should make dispatcher behavior explicit. Each test should cover
a concrete routing, validation, config, or provider-command construction
contract.

## Current State

The current repository is a pre-release scaffold. Some tests intentionally fail
because they describe the genericization work still required.

At the scaffold checkpoint, the focused tests reported:

```text
7 failed, 6 passed
```

Those failures are expected until source-project defaults and aliases are
removed.

## Running Tests

```bash
python -m pytest tests -q
```

Run focused files during extraction:

```bash
python -m pytest tests/test_public_defaults.py -q
python -m pytest tests/test_generic_router.py -q
python -m pytest tests/test_cli.py -q
```

## Test Categories

Expected test areas:

- CLI help and config loading.
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
