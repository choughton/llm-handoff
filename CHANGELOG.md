# Changelog

All notable changes to `llm-handoff` will be documented here.

This project follows a simple pre-release changelog until the first public tag.

## Unreleased

- An audit verdict whose strings hold unescaped backslashes, such as a Codex
  quality reviewer's `rg -n '12\.'` in `commands_run`, now parses instead of
  failing `quality_verdict_malformed`. Each lone backslash is read as a
  literal one; valid JSON escapes keep their meaning.
- Behavior change: in `pr` mode, saved `## Review Responses` now last for the
  whole correction round. A later claim in the round merges its responses
  into the saved ones by finding number, and a claim without responses keeps
  them. Previously a claim without responses, such as the implementer's
  handback after an audit bounce, deleted the saved ones, so the fix's
  answer was never posted and the merge stopped. Saved responses are dropped
  when a new correction round starts. A saved Codex finding left without a
  response now goes to the planner's one deferred-findings pass, with its
  thread still open and the story state kept, when it and deferred findings
  are the only unresolved threads. Previously the dispatcher paused and
  cleared the story state, so a restart could not recover the open pull
  request. A person's comment or any other unresolved thread still pauses.

## Pre-release snapshot - 2026-10-19

Condensed history from the start of the extraction (2026-04-20) through
2026-10-19. Each entry states the behavior at the snapshot date; features
that were added and later replaced or removed in that window appear in their
final form. Mechanism detail lives in the pull requests, `PROJECT_STATE.md`,
and `CONFIGURATION.md`.

### Added

- Extraction scaffold: the `llm_handoff` module, public README and repository
  instructions, the documentation split, generic handoff prompts and agent
  templates, and the ported dispatcher tests as the full-suite gate.
- Packaging: `pyproject.toml` (hatchling) installs from a Git checkout with
  `pip install .` and adds the `llm-handoff` console command. The wheel
  bundles `examples/reference-workflow/`. Not published to PyPI.
- `llm-handoff init` copies the reference workflow into a target repository,
  keeps user content through managed sections, merges Claude settings, and
  refuses linked or hard-linked destinations, even with `--force`.
- `init --provider claude|codex|antigravity` puts every role on one CLI; with
  no `--provider`, a machine with only one provider CLI gets that mapping. The
  reference workflow ships Claude, Codex, and `agy` helper files for each role.
- `init` lists candidate build and test commands it finds (CI, `package.json`,
  `pyproject.toml`, `Makefile`, Cargo, Go) without running them;
  `--verification-command` confirms one into `verification_allowed_prefixes`.
- `init --tracker linear --tracker-team <key>` with `--tracker-project` or
  `--tracker-label` sets up a fresh repository in Linear mode: a validated
  `tracker` block plus Linear blocks in `AGENTS.md` and the handbook.
- `llm-handoff doctor` checks a checkout before a run, one `PASS`, `FAIL`, or
  `WARN` line per check, exits 1 on any error, and writes no files. The
  supervisor's `--doctor` mode shares its checks.
- Doctor checks cover Git history, provider binaries, the handoff file, the
  normalizer, scanners, the work-claim gate and Verification allowlist,
  interpreter grants per launch, `agy` settings, and Linear tracker connectors.
- `llm-handoff doctor --probe-tracker <ISSUE>` (Linear mode, opt-in) launches
  one locked-down Claude role to check that the issue and the configured
  workflow states can be read.
- `llm-handoff stop` asks a running dispatcher to finish the cycle in flight
  and exit 0. It refuses when no live dispatcher holds `.dispatch.lock` or
  Git does not ignore `logs/dispatch/`. Ctrl+C still aborts at once.
- Operator prompt at pauses (`interactive_pause`, on by default): at a TTY,
  Enter, free-text planner guidance, `retry`, `quit`, or `@backend`,
  `@frontend`, or `@auditor` with an optional message.
- `pause_recovery` (`planner` by default, or `"off"`): the six non-user pauses
  get one bounded planner recovery that carries the pause reason and the
  validator's diagnosis. A `user` route still waits.
- Deterministic work-claim gate (`enforce_work_packet`) checks
  `ready_for_review` claims against the Work Packet before the auditor runs; a
  pass writes `## Dispatcher Verification` so reviewers do not re-run it.
- Work-claim `scanners` (gitleaks, semgrep, bandit, pip-audit, or any tool)
  with diff-scoped placeholders, per-scanner timeouts, and `required`.
- `verification_allowed_prefixes`: the gate runs only Verification commands
  that match a listed prefix and contain no shell metacharacters; others fail
  `verification_not_allowed`. An empty list logs `WARN` at every start.
- `commit_mode: dispatcher`: implementers leave changes uncommitted and the
  dispatcher commits only in-scope files, recording `scope_sha`. Requires the
  work-claim gate.
- In dispatcher commit mode, uncommitted implementer work carries into the
  next dispatch for the same story, and the record is saved to
  `logs/dispatch/carry-over.json` so a restart keeps it.
- `publish.mode` (`local`, `merge-commit`, `pr`): branch modes put each story
  on an `llm-handoff/<story>` branch and merge it with a report commit or a
  pull request. Agents never branch, push, or merge. Default `local`.
- `publish.wait_for_reviewers: [codex]` (`pr` mode with `auto_merge`) waits
  for Codex's GitHub review of the pushed commit; findings bounce to the
  implementer for one correction round (`max_review_rounds: 1`).
- Review responses: each `## Review Responses` entry is posted as the reply on
  its review thread and the thread is resolved. `pr` mode auto-merges only
  when every thread is resolved.
- Deferred findings: a `Deferred:` response keeps its thread open, and a pull
  request held only by deferred findings gets one planner pass to widen the
  packet, accept them (`## Deferred Finding Decisions`), or route to `user`.
- Audit fan-out (`audit.reviewers: [quality, security]`): read-only quality
  and security reviewers run in parallel on the story range. Each cycle writes
  `usage.json` with per-reviewer tokens and cost.
- `audit.security_model` sets the Claude security reviewer's model and is
  rejected for other providers. The `Audit reviewers:` start line names the
  model and its source.
- Role `fallback`: a timeout, rate or usage limit, sign-in failure, or launch
  failure reruns the dispatch once on the fallback. `idle_timeout_ms` (off by
  default) kills an agent that prints nothing for that long.
- `protected_paths`: a dispatch that changes control files (`AGENTS.md`,
  handoff prompts, dispatch config, provider agent directories) outside its
  Work Packet routes to `user` with `protected_file_modified:`.
- `antigravity` provider (binary `agy`): print mode with stream-json output,
  `--print-timeout`, optional `--model`, and `--conversation` resume. It gets
  no provider environment variables or API key.
- The `antigravity` adapter fails a run whose stderr reports a print-mode
  permission auto-deny, naming the `permissions.allow` entry to add, and
  treats `authentication required` as a sign-in failure.
- `agy` agent files under `.agents/agents/` for every role and for
  `supervisor-repair`, each with a `tools:` list (`handoff-validator` gets no
  write tools). `.agents/**` is a default protected path.
- `agy` permission entries (`llm_handoff/antigravity_permissions.py`): `init`
  prints them for `~/.gemini/antigravity-cli/settings.json`, and `doctor`
  checks that file. Neither command writes it.
- `audit.security_provider: antigravity`, a read-only `agy` security reviewer.
  The automatic order is `claude`, `codex`, `antigravity`. It passed an
  operator live check on 2026-10-18 with `agy` 1.4.2.
- Next-agent normalizers `antigravity` (default model `gemini-3.8-flash-low`)
  and `codex` (`codex exec --sandbox read-only`, CLI login, no API key). Both
  fail closed on unusable output; the default stays `claude`.
- Linear mode (`tracker.provider: linear`, off by default): a validated
  `tracker` block, handoff lease fields (`tracker_issue`, `tracker_url`,
  `selection_basis`) kept in story state, and shipped Linear prompt templates.
- Linear-mode Claude launches get the role's Linear MCP tools, and
  `tracker.claude_mcp_config` passes `--mcp-config <path> --strict-mcp-config`.
  Without it, startup warns that every user MCP server loads.
- Linear-mode prompts state the planner selection rules, the issue-state
  transitions and evidence comments, and a read-only validator evidence check.
  `ISSUE_TEMPLATE.md` gives an example issue. Issue text is untrusted data.
- `handoff-validator` helpers report a `LINEAR-EVIDENCE` row; in Linear mode a
  failed, missing, or unparsed row blocks automatic planner pause recovery.
- `docs/TESTING.md` `## Linear Mode Acceptance Run`: a manual operator
  runbook against a sandbox Linear team. The test suite does not run it.
- `remaining_stories_dropped`: validation fails a rewrite that drops planned
  stories not listed under `## Dropped Stories` with a reason. A
  `remaining_story_not_in_project_state` warning flags unknown stories.
- An audit dispatched with `@auditor <message>` records the acceptance time
  and the message's SHA-256 (never its text) in `review.json`, the audit
  handoff, and the pull request body.
- Integration tests (`tests/integration/`, marker `integration`) install the
  wheel offline and run `init`, `doctor`, and `--dry-run` on throwaway Node
  and Go repositories.

### Changed

- Behavior change: the work-claim gate is on by default. Opting out takes
  `enforce_work_packet: false` plus `work_packet_opt_out_reason`, and logs a
  `WARN`. Planner assignments now need a parseable `## Work Packet`.
- Behavior change: the security reviewer runs by default. Opting out takes
  `audit.reviewers: [quality]` plus `audit.security_opt_out_reason`. Startup
  fails with `security_reviewer_failed:` when the reviewer CLI is missing.
- With `audit.security_provider` unset, the reviewer is the first installed
  CLI that differs from the implementer's provider, else the implementer's
  own vendor with a `WARN` that the review is not independent.
- Behavior change: claims are checked against Git. `ready_for_review` needs
  `scope_sha`, an unknown `Commit SHA verified` fails `evidence_sha_unknown:`,
  and the gate adds `evidence_sha_mismatch` and `no_changes` in both modes.
- Behavior change: `planner` and `frontend` default to `antigravity`
  (`binary: agy`). A config with no `agents:` block needs `agy`; a Linear
  config that omits either role fails to load, so map both to claude or codex.
- Behavior change: `init` provider auto-detect looks for `claude`, `codex`,
  and `agy`; a `gemini`-only machine gets the reference mapping.
  `init --tracker linear` refuses the all-Antigravity mapping.
- Behavior change: the default supervised repair scope is `docs/`,
  `.claude/agents/`, `.codex/skills/llm-handoff/`, and `.agents/agents/`.
  Other paths need `--repair-allow-path`.
- Behavior change: a security `fail` whose findings are all below
  `audit.block_severity` passes the audit, listing them in `## Security Notes`.
  A quality `fail`, or a security `fail` with no findings, still blocks.
- Behavior change: a failed Codex review gets one `@codex review` retry (none
  after a usage limit). If that fails or times out, a terminal prompt offers
  `merge`, `retry`, or `quit`; without a terminal the loop stops.
- Linear mode rejects `provider: antigravity` on any role or fallback at
  config load (`agy` agent files give roles no Linear MCP access), and
  `init --tracker linear` maps `planner` and `frontend` to `claude`.
- In Linear mode, a missing or empty `HANDOFF.md` dispatches one planner
  lease rebuild (`tracker_lease_rebuild`) instead of exiting 1; a second empty
  read pauses.
- Default Claude models: `claude-opus-5-5` for roles and `claude-haiku-5-5`
  for the normalizer. Explicit `model` values are unchanged.
- `pr` mode merges the base branch into the story branch before each push, and
  once more if `gh pr merge` reports it not mergeable. A conflict pauses with
  the files named and the story state kept.
- A resumed `pr` publish pushes only when every commit added since the pause
  is a clean merge of the base branch (checked with `git merge-tree`).
  Anything else pauses and pushes nothing.
- Resume pause messages explain merge parent order and name the conflicted
  files a merge changed. `CONFIGURATION.md` explains that merge drivers such as
  `union` run during the resume check and can make it pause.
- A handoff that moves on from a saved story needs that story's pull request
  merged; an open one on its reviewed head gets one planner pass, and anything
  else pauses. This replaces the `Story branch not ready` exit.
- The pull request and merge-commit report is a short page: a Summary of up to
  three sentences and Reviewer notes (`### Story`, `### Acceptance checks`,
  `### Audit`). Changes, Test plan, Work Packet, and token usage are gone.
- The audit range starts at the story's first implementer dispatch, and in
  branch publish modes the audit and gate base survive a restart through
  `logs/dispatch/publish/story.json`.
- An audit whose range has no committed changes, such as an epic close, runs
  the Work Packet Verification first. A failure, or a blocked audit on such a
  range, routes `verified_fail` to `planner`.
- A handoff whose `status` contradicts its `next_agent` fails validation
  (`frontmatter_status_route_conflict`); prompts pair each status with its
  route.
- A post-dispatch validation failure logs an `ERROR` naming the errors and
  pauses instead of exiting 1 silently. Claims the gate has not checked never
  get automatic planner recovery.
- `EVIDENCE_MISSING` errors, including `Commit SHA verified: HEAD`, go
  through the one-shot producer hygiene repair instead of stopping the run.
- Verification Evidence fields accept indented continuation lines and a
  parenthetical note in the label.
- An audit reviewer that fails to start stops the run after one attempt with
  an `audit_reviewer_start_failed:` line. A missing verdict is never a pass.
- A provider sign-in failure stops the run at once with that CLI's sign-in
  steps instead of retrying, unless the role's `fallback` takes over.
- The console shows the work-claim gate's `work-claim:` progress lines;
  command output tails stay in the log.
- Token usage and cost moved from the console to `DEBUG` `usage:` log lines.
- A `.dispatch.lock` refusal names the lock's pid, its start time, and whether
  it is running. The lock is never removed automatically.
- Claude roles are told their shell allowlist up front; a refused command
  logs a one-line `WARN`.
- The dispatcher pauses before sending a Claude `backend` or `frontend` in
  `dontAsk` mode a Work Packet that scopes a `.claude` path Claude Code would
  refuse to write.
- In dispatcher commit mode, planner and finalizer commits that hold only the
  handoff are undone with `git reset --soft`; other planner commits or branch
  changes stop the loop.
- Prompts: planners route a finished epic to the auditor with
  `close_type: epic`, the auditor routes an approved last story to
  `finalizer`, and implementers keep the assignment's story fields unchanged.
- Planner prompts ask for one Verification command per list item with no
  prose, and never a temp directory inside the repository.
- The finalizer prompt and helpers name the configured `project_state_path`.
- Documentation: the README is organized around getting started, the
  repository map moved to `docs/INVENTORY.md`, each first-run guide has one
  job, and copied files no longer link to llm-handoff's own docs.
- The test suite runs on 8 `pytest-xdist` workers, and an autouse fixture
  fails any test that launches a real provider CLI.
- Package source may name the Antigravity (`agy`) CLI; `antigravity` is still
  rejected as a `next_agent` value.
- Removed `auto_push`, which was never read. `auto_push: false` is ignored;
  any other value fails at load and points to `publish.mode`.
- Removed Gemini CLI support (it no longer serves personal Google accounts):
  the `gemini` provider, security reviewer, normalizer, `init` mapping, and
  `.gemini/` files. A `gemini` value fails at load, pointing to `antigravity`.
- With the Gemini removal, `tenacity` and `google-genai` are dropped, and
  `doctor` no longer checks a container runtime. `GEMINI_API_KEY` and
  `GOOGLE_API_KEY` are still stripped from every launch.
- Behavior change: `planner_api_key_env` and `--use-planner-api-key-env` are
  removed. `planner_api_key_env: false` is ignored, any other value fails to
  load, and the flag exits 2 before any config loads.

### Fixed

- Prose in a Work Packet no longer breaks the gate: a prose-only or
  `:`-ending Verification line is `work_packet_unparseable`, and a repeat
  failure of the same command bounces to the planner.
- A path in both `Files in scope` and `Files out of bounds` is
  `work_packet_unparseable` instead of looping the implementer; a new change
  to a disallowed file is a `scope_violation` to revert.
- Path lists written as prose no longer create stray out-of-bounds entries.
  The narrowest matching entry decides, in the gate and the protected-file
  check.
- Branch publish modes refresh the saved Work Packet on each implementer
  dispatch, so a later planner revision is no longer lost.
- `pr` mode saves `## Review Responses` from the handoff it sends to the
  auditor, so a hand-fixed correction round no longer loses them.
- A resumed `pr` publish that conflicts again at the merge step keeps the
  merge-step marker and says the pull request was left open.
- A refused base-branch merge names git's `error:` or `fatal:` line and any
  blocking local files. The dispatcher never stashes, discards, or commits them.
- A start on a story branch whose pull request already merged fast-forwards
  and switches to the base branch when it is safe, instead of stopping after a
  planner cycle. Nothing is pushed.
- A passing epic-close audit with no story branch, or with nothing beyond the
  remote base, publishes nothing and continues to the finalizer.
- A restart that routes straight to the auditor, or a correction round with no
  new commit, now reviews the whole story instead of one commit or nothing.
- An operator `@backend` or `@frontend` on a handoff with no `story_id`
  continues the saved story; a branch that is not ready pauses again.
- Audit verdict JSON with raw newlines or tabs inside strings parses instead
  of failing `quality_verdict_malformed`.
- `doctor` and the supervisor preflight accept the finalizer's `verified_pass`
  epic-close handoff instead of reporting `EVIDENCE_MISSING`.
- `init --provider codex` writes `commit_mode: dispatcher`, because Codex's
  sandbox keeps `.git` read-only and agent mode stalled.
- On Windows, npm-installed CLI shims launch through their `node <entry>.js`
  target, so arguments with `"`, `<`, `>`, `|`, `&`, `%`, or `^` arrive intact.
- Codex launches resolve the output schema from the installed package, and a
  Codex validator or finalizer report is read from its JSON `summary`.
- The security reviewer's verdict schema no longer has a `$schema` key, which
  Claude Code's `--json-schema` rejected.
- The one-shot handoff repair reads `git status` correctly and leaves the
  handoff uncommitted in dispatcher commit mode.
- Recording the commit SHA replaces only the `pending-dispatcher-commit`
  marker, and a blocked handback with the marker gets a `sha_pending` warning
  instead of a `sha_missing` error.
- A Work Packet field written as nested bullets no longer warns that it is
  empty, and an empty Evidence field no longer absorbs the next one.
- A scheduled planner recovery dropped because `HANDOFF.md` changed logs a
  `WARN`.
- Agent launches keep `USERNAME`, `USER`, and `LOGNAME`, so pytest no longer
  uses a shared `pytest-of-unknown` temp directory.
- Supervised child imports are isolated from the target working directory.
- Supervised repair with an `antigravity` role launches the shipped
  `supervisor-repair` agent and ignores an `agent_name` override.
- Codex usage-limit stops name the limit; the pull request is left open.
- Wording and ASCII fixes in the config templates, the finalizer skill,
  `CONFIGURATION.md`, and `tests/test_cli.py`.

### Security

- Provider CLIs launch with an allowlisted environment. `env_passthrough` adds
  names, `env_mode: inherit` restores the copy, and Codex roles need
  `use_api_key_env: true` for `OPENAI_API_KEY`. Security reviewers too.
- Behavior change: the gate's Verification command and scanners also run with
  an allowlisted environment; vendor API keys never reach them. A command
  that needs an inherited variable must list it in `work_claim_env_passthrough`.
- Safer launch flags: Claude Code runs `--permission-mode dontAsk` with an
  `allowed_tools` list and web tools denied; Codex runs `workspace-write`.
  `unsafe_bypass: true` restores the old flags with a startup warning.
- The reference workflow ships `.claude/settings.json` deny rules with a
  `PreToolUse` hook, and `.codex/rules/llm-handoff.rules`, which forbids
  `git push`. `SECURITY.md` has a Threat Model section.
- Breaking default change: interpreter grants are an opt-in. Default Claude
  and `agy` grants omit `python`, `python3`, and `pytest`, and
  `interpreter_grants: true` restores them. Role allowlists are a guardrail.
- `agy` allow entries are anchored regex grants that cover arguments, and the
  deny entries refuse any `git` command line with ` push`. `doctor` fails a
  regex allow entry naming `git` that it cannot prove push-safe.
- An `agy` deny-rule refusal fails the dispatch even when the role recovers.
  The log line names the step and the matched rule, never the command line.
- The work-claim gate matches Verification commands literally (single spaces
  only), the same rule the `agy` grant applies.
- Provider stderr is redacted (`llm_handoff/redaction.py`) before it reaches
  the log or a reviewer error. Raw stderr files are unchanged.
- The `antigravity` normalizer and security reviewer run from private temp
  directories with fixed tool lists and `commandExecutionPolicy: off`, accept
  only `structured_output`, and never echo `agy` output in errors.
- Console lines escape terminal control and format characters before
  printing; the log keeps the exact text.
- Operator-prompt instructions quote the pause reason and operator message
  between fixed markers that the quoted text cannot close.
- Behavior change: config load rejects a `handoff_path` or
  `project_state_path` that leaves the repository (absolute, UNC, `..`, the
  root itself, or a symlink escape). These were accepted before.
- Behavior change: a carry-over record loaded at startup applies only after a
  `Carry these over as <story>'s work? [y/N]` prompt at a terminal. Any other
  answer discards and deletes it with a `WARN`; a dry run keeps the file.
- Behavior change: the dispatcher no longer follows a symlink or junction at
  or above the carry-over record. It writes and deletes the record through
  pinned directory handles, or keeps it in memory where it cannot.

## 0.0.0

- No public release tag has been published yet.
