# Testing

The test suite should make dispatcher behavior explicit. Each test should cover
a concrete routing, validation, config, or provider-command construction
contract.

## Current State

The full dispatcher test suite runs as one public gate, including the ported
migration coverage and the focused public-default tests.

One test, `test_timed_out_cmd_shim_child_is_killed_with_its_tree` in
`tests/test_process_tree.py`, launches a real Windows `.cmd` shim process tree
and is skipped on other platforms. Two tests in `tests/test_npm_shim_launch.py` launch a fake npm `.cmd` shim
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

## Linear Mode Acceptance Run

This is a manual runbook for checking optional Linear mode end to end. An
operator follows it by hand; the test suite never runs it, and nothing here
is automated. Run it only against a sandbox Linear team and a disposable
target repository. Never run it against a production team or a repository
whose history matters: the run posts comments, changes issue states, and
creates commits.

The dispatcher never calls Linear. Agents read and write the issues through
their own CLI's Linear MCP connector, so every Linear-side result below is
checked by the operator in Linear directly. The placeholders in this section
are `<target>` (the disposable repository path), `<KEY>` (the sandbox team
key), and issue identifiers such as `ENG-123`; substitute your own sandbox
values and never paste a real token into a file you commit.

### Prerequisites

- A sandbox Linear team whose workflow states include the names you will
  use for `eligible_state`, `in_progress_state`, and `success_state`, plus
  `blocked_state` if you set it. `init` writes `eligible_state: Todo`,
  `in_progress_state: In Progress`, and `success_state: In Review`; edit
  `dispatch_config.yaml` if your sandbox team uses other names.
- A Linear project or a label in that team that defines the pool for this
  repository. Use exactly one of them.
- At least five issues in `eligible_state` in that pool, each written from
  the issue template in
  `llm_handoff/prompt_templates/linear/ISSUE_TEMPLATE.md` (`init` also
  renders it into the target's `docs/handoff/HANDBOOK.md`). One is for
  Scenario A, one for Scenario B, one for Scenario C, and two for
  Scenario D, whose epic needs a second story.
- A disposable target repository with at least one commit and a test command
  the repository already defines, so a Verification command can run it.
- `enforce_work_packet: true` in the target's `dispatch_config.yaml`. Linear
  mode rejects a config without it.
- The provider CLIs your role mapping uses, signed in, each with a Linear MCP
  server configured (see Connector Setup).

### Setup

Preview the init first, then run it:

```bash
llm-handoff init <target> --tracker linear --tracker-team <KEY> --tracker-project <name> --dry-run
llm-handoff init <target> --tracker linear --tracker-team <KEY> --tracker-project <name>
```

Use `--tracker-label <name>` instead of `--tracker-project <name>` when a
label defines the pool. Give exactly one of the two. `--tracker-mcp-server
<name>` sets `tracker.mcp_server` and defaults to `linear`. `init --tracker
linear` sets up only a repository that has no `dispatch_config.yaml` yet.

Record the `commit_mode` value in the generated `dispatch_config.yaml`. The
expected `implementation_handoff` evidence in Scenario A depends on it.

### Connector Setup

Configure the Linear MCP server for each provider CLI that the role mapping
launches. The files are the ones "Tracker (Optional Linear Mode)" in
`CONFIGURATION.md` names. In every case the server name must equal
`tracker.mcp_server`.

- Claude: `<target>/.mcp.json`, the user file `~/.claude.json`, or a file
  named by `tracker.claude_mcp_config`. Set `tracker.claude_mcp_config` for
  this run: Claude launches then pass `--strict-mcp-config` and load only
  that file's servers, and the probe is limited to them.
- Codex: an `[mcp_servers.<server>]` table in `config.toml`
  (`$CODEX_HOME/config.toml` or `~/.codex/config.toml`, or
  `<target>/.codex/config.toml`).

Linear mode does not support `antigravity` roles.

### Preflight

1. Run `llm-handoff doctor --repo-root <target>`. Expect one
   `tracker connector` line per launch group of the roles and fallbacks: up
   to two for Claude (`claude strict` for Claude launches that load
   `tracker.claude_mcp_config`, `claude` for the others, when both kinds
   exist) and one for Codex. Fix any FAIL. A WARN means `doctor` did not
   find the server in the files it reads.
2. Run `llm-handoff doctor --repo-root <target> --probe-tracker ENG-123`
   with one of the sandbox issue identifiers. Expect a `tracker probe` PASS:
   the issue was read and every configured state name is one of the team's
   workflow states.
3. Hook check. Back up the target's `.claude/settings.json`, then add a
   project hook that leaves a marker file, for example a `SessionStart` and
   a `UserPromptSubmit` hook whose command creates `hook-fired.txt` in the
   target root. Run the probe from step 2 again and confirm that
   `hook-fired.txt` does not exist: the probe passes
   `--setting-sources user` and `--settings '{"disableAllHooks": true}'`,
   so project hooks must not fire. Then restore the backed-up settings file
   and delete any marker file.
4. Run `llm-handoff --repo-root <target> --dry-run` and confirm it
   completes without errors.

### Scenario A: One Passing Issue

Start the loop with `llm-handoff --repo-root <target>`. Let it lease one
issue and take it through the auditor. Record, in order, on the leased
issue:

1. A `lease_opened` evidence comment from the planner, and the issue moving
   to `in_progress_state`.
2. An `implementation_handoff` comment from the implementer. Under
   `commit_mode: dispatcher` it carries
   `commit_sha: pending-dispatcher-commit`. Under `commit_mode: agent` it
   carries the concrete commit SHA.
3. An `audit_passed` comment that carries the concrete `scope_sha` from
   `HANDOFF.md` as `commit_sha`.
4. The issue moving to `success_state`, after the `audit_passed` comment.

With the audit fan-out on (the default `audit.reviewers: [quality,
security]`), also confirm:

- The fan-out quality reviewer posted no comment: there is exactly one
  `audit_passed` comment for that `commit_sha`.
- The post-verdict Linear evidence step posted `audit_passed`: the cycle's
  `logs/dispatch/audit/<timestamp>-cycle<N>/linear-evidence.log` holds a
  JSON object with `"linear_evidence": "posted"`, the leased issue, and
  `audit_passed` as the last event.

### Scenario B: One Forced Audit Failure

Force one audit failure on a second issue. The Verification command must
pass: with `enforce_work_packet: true` the work-claim gate runs it before
any reviewer, and a failing command is bounced to the implementer as
`verification_failed` without reaching the auditor, so it produces no
`audit_failed` comment. Instead, write an issue whose Verification command
is an existing repository test that passes, and whose Objective asks for a
committed change an audit reviewer blocks. For example, with the default
`audit.reviewers: [quality, security]`, ask for a helper that builds a
shell command from a user-supplied string and runs it with `shell=True`;
the security reviewer reports that at or above `block_severity`. If the
implementer declines the change, or routes to the planner with
`blocked_implementation_failure` instead of handing back for review, record
that as a deviation.

Record:

- An `audit_failed` comment on the issue that lists the blocking findings
  and carries the concrete `scope_sha` as `commit_sha`.
- The issue stays in `in_progress_state`; it does not move to
  `success_state`.
- `HANDOFF.md` routes back to the implementer with
  `status: verified_fail` and the same `tracker_issue`.

Stop the loop with `llm-handoff stop --repo-root <target>` once the failure
is recorded, or let the implementer retry and record the outcome. Without
`--repo-root`, `stop` resolves the repository from the current directory
and reports nothing to stop when you run it from outside `<target>`.

### Scenario C: Deleted Lease

Between assignments (stop the loop with
`llm-handoff stop --repo-root <target>` first, and wait for the dispatcher
to exit), delete
`docs/handoff/HANDOFF.md` in the target, or empty it, then start the loop
again with `llm-handoff --repo-root <target>`.

- Expect one `WARN` line and one planner cycle with routing source
  `tracker_lease_rebuild`. The planner rebuilds the lease from the pool: it
  picks an issue and posts `lease_opened`, or routes to `user` with
  `status: blocked_missing_context` when no issue qualifies.
- A second miss in a row pauses the loop: if `HANDOFF.md` is still missing
  or empty at the next cycle, the dispatcher pauses until it has content,
  with no second planner dispatch and no pause recovery. A cycle that reads
  content resets this, so the second miss is seen only when the rebuild
  planner leaves the handoff missing or empty. Record it if it occurs.

### Scenario D: Configured tpm

Stop the loop with `llm-handoff stop --repo-root <target>` and wait for the
dispatcher to exit. Add an `agents.tpm` entry to the target's
`dispatch_config.yaml`, for example the commented `tpm` block in
`dispatch_config.example.yaml` (`provider: codex`; Linear mode does not
support `antigravity` roles), and run `llm-handoff doctor --repo-root <target>` to confirm it no longer reports
`role tpm: not configured (planner picks stories)`.

`init --tracker linear` renders only the tpm-absent prompt text, so back up
the target's `docs/handoff/HANDBOOK.md` and edit the block between the
`<!-- llm-handoff:linear:begin -->` and `<!-- llm-handoff:linear:end -->`
markers: replace the `## Linear Mode: planner` section with the contents of
`llm_handoff/prompt_templates/linear/roles/planner_with_tpm.md`, and add the
contents of `llm_handoff/prompt_templates/linear/roles/tpm.md` as a new
section inside the markers. Then write a fresh planner handoff that asks
for a new epic of at least two stories, one per Scenario D issue, and start
the loop with `llm-handoff --repo-root <target>`. The second story must stay
in `remaining_stories` after the tpm assigns the first: with
`remaining_stories: []` the audit treats the pass as an epic close and routes
to `finalizer`, not `tpm`.
Do not empty `HANDOFF.md` for this scenario: a missing lease goes to the
planner's lease rebuild (Scenario C), which leases an issue itself. Check by
hand:

- The planner opens the epic and routes to `tpm` without leasing an issue:
  its handoff has `next_agent: tpm` and no `tracker_issue`.
- The tpm leases the eligible issue its selection rules rank first: it posts a `lease_opened` comment, moves
  the issue to `in_progress_state`, and writes the `## Work Packet` in
  `HANDOFF.md` with `producer: tpm` and the other story still listed in
  `remaining_stories`. If `remaining_stories` is empty, the route check
  below does not apply; record that as a deviation.
- After the implementer and the audit, `HANDOFF.md` routes to `tpm` with
  `status: verified_pass`, not to `planner`, and the issue has an
  `audit_passed` comment that carries the concrete `scope_sha` as
  `commit_sha`.

Stop the loop once the route to `tpm` is recorded. Remove `agents.tpm` and
restore the backed-up `HANDBOOK.md` before rerunning Scenarios A to C.

### Validator Check

1. On an issue with a current lease, edit one field of the latest evidence
   comment in Linear (for example `next_agent` or `commit_sha`) so it no
   longer matches `HANDOFF.md`. Leave the handoff as it is.
2. Run a validation-only turn: the validator report the dispatcher runs,
   read-only, at one of the pauses listed under Pause Recovery in
   `CONFIGURATION.md`, for example a stale route on an unchanged handoff.
   The dispatcher has no command that runs one on demand.
3. Expect the report's `LINEAR-EVIDENCE` row to be `FAIL`, naming both the
   comment value and the handoff value of the edited field.
4. Expect the dispatcher to wait for a person. With
   `pause_recovery: planner` it logs
   `Linear evidence check failed; not scheduling planner recovery.` and
   does not dispatch planner pause recovery.
5. Restore the comment field before you continue.

### Caveats

- The post-verdict Linear evidence step fails closed when another process
  writes to an ignored path while the step runs, for example
  `.pytest_cache`, editor swap files, or build output. Close test watchers,
  editors, and builds on the target before an audit runs. A failure logs a
  `linear_evidence_failed:` line and stops the loop with exit 1.
- The step's checks only detect a repository file change after the fact.
  They do not prevent one.
- The dispatcher never calls Linear and cannot verify a Linear write. Check
  every comment and state change on the issue in Linear directly.
- The validator's evidence check is an agent check, not a dispatcher check.

### What To Record

Record one row per issue, plus any deviation from the expected results.

| Item | Value |
|---|---|
| Tracker issue | `ENG-123` |
| Scenario | A, B, C, or D |
| Evidence comment events seen, in order | `lease_opened`, `implementation_handoff`, ... |
| State transitions | `Todo` -> `In Progress` -> `In Review` |
| `scope_sha` of each passed story | concrete SHA |
| Audit log folders | `logs/dispatch/audit/<timestamp>-cycle<N>/` |
| Doctor and probe results | PASS, WARN, or FAIL per line |
| Hook check | marker file absent or present |
| Deviations | what differed from the expected result |
