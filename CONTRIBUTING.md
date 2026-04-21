# Contributing

`llm-handoff` is a reference implementation, not an open-ended framework
project. Contributions are welcome when they make the reference implementation
clearer, safer, or easier to inspect.

## Good Contributions

- Small bug fixes.
- Tests that capture dispatcher failure modes.
- Documentation improvements that clarify setup or design constraints.
- Provider CLI adapter fixes for documented CLI behavior changes.
- Genericization work that removes project-specific names or assumptions.

## Out Of Scope

- Web UI work.
- Service daemon or hosted orchestration.
- Database-backed queues.
- Parallel branch or worktree orchestration.
- Plugin marketplaces.
- Non-Git state backends.
- Proprietary prompt text, private project logs, or internal workflow details.

Open an issue before proposing broad architecture changes.

## Development Setup

See [INSTALL.md](INSTALL.md).

```bash
python -m pip install -r requirements-dev.txt
pre-commit install
```

Run the local hooks before opening a PR:

```bash
pre-commit run --all-files
```

## Tests

Run the relevant focused tests before opening a PR:

```bash
python -m pytest tests -q
```

The full test suite is intentionally not part of pre-commit. Run it manually
before sending changes, and expect CI to enforce it once CI is wired.

If a test is expected to fail because the extraction is incomplete, state that
clearly in the PR and point to the next required implementation step.

## Style

- Keep code and docs generic.
- Prefer explicit failure over silent fallback.
- Keep comments short and useful.
- Avoid adding dependencies unless the dispatcher needs them at runtime or for
  a focused test.
- Keep public docs conservative. Do not describe the dispatcher as self-healing
  or production-ready for arbitrary repositories.

## Commits

Use descriptive commit messages. A good commit explains what changed, why it
changed, and how it was verified.

## Security Issues

Do not open public issues for security-sensitive reports. See
[SECURITY.md](SECURITY.md).
