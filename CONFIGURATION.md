# Configuration

`llm-handoff` is expected to use a YAML config file named
`dispatch_config.yaml` in the target repository.

This document describes the public configuration surface currently parsed by
the source checkout. Runtime role-to-provider dispatch is config-driven for the
registered CLI adapters. See [dispatch_config.example.yaml](dispatch_config.example.yaml)
for a copyable example shape.

## Minimal Shape

```yaml
handoff_path: docs/handoff/HANDOFF.md
project_state_path: PROJECT_STATE.md
auto_push: false
backend_resume: true
planner_resume: true
planner_api_key_env: false

agents:
  planner:
    provider: gemini
    binary: gemini
    resume: true
    timeout_ms: 1200000

  backend:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: true
    timeout_ms: 1200000

  auditor:
    provider: claude
    binary: claude
    model: claude-opus-5-5
    resume: false
    timeout_ms: 900000
```

## Handoff Path

Default:

```yaml
handoff_path: docs/handoff/HANDOFF.md
```

Flat alternative:

```yaml
handoff_path: HANDOFF.md
```

The nested path is the recommended default because it leaves room for handoff
examples, archive files, and prompt handoff helpers. The flat path is useful for
small repos that prefer fewer directories.

## Project State Path

Recommended default:

```yaml
project_state_path: PROJECT_STATE.md
```

This file is an optional durable project-status document. Consumers can point it
at an existing file if their workflow already uses one.

## Auto Push

Default:

```yaml
auto_push: false
```

Remote pushes should be explicit. The dispatcher may create local commits as
part of a finalizer workflow, but it should not publish to a remote unless a
user explicitly opts in.

`auto_push` is parsed but not read by the current dispatcher. The dispatcher
itself never pushes, and setting `auto_push: true` has no effect. Agents are
told not to push, and the reference workflow denies `git push` through
provider guardrails, but neither is a hard boundary. Keep push credentials out
of the dispatch environment; see [SECURITY.md](SECURITY.md#threat-model).

## Runtime Controls

The dispatcher exposes role-based runtime toggles for session reuse and planner
API-key environment handling:

```yaml
backend_resume: true
planner_resume: true
planner_api_key_env: false
```

Provider-named runtime keys from the source extraction are not supported in the
public config. Use the role-based keys shown above.

When `planner_api_key_env` is `false`, planner launches strip
`GEMINI_API_KEY`. `GOOGLE_API_KEY` is always stripped for planner launches.
When `planner_api_key_env` is `true`, `GEMINI_API_KEY` is preserved, but
`GOOGLE_API_KEY` is still stripped.

## Work-Claim Gate

On by default:

```yaml
enforce_work_packet: true
work_packet_verification_timeout_seconds: 600
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enforce_work_packet` | `true` | Runs the work-claim gate. Setting it to `false` needs `work_packet_opt_out_reason`. |
| `work_packet_opt_out_reason` | unset | Required, non-empty text when `enforce_work_packet` is `false`; rejected while the gate is on. |
| `work_packet_verification_timeout_seconds` | `600` | Timeout in seconds for the Verification command. |
| `work_claim_env_passthrough` | `[]` | Extra environment variable names the Verification command and scanners receive. They always get only `PATH`, `HOME`, `USERPROFILE`, `SystemRoot`, `SystemDrive`, `TEMP`, `TMP`, `LANG`, `LC_ALL`, `TERM`, `USERNAME`, `USER`, `LOGNAME`, `COMSPEC`, `PATHEXT`, `WINDIR`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, and `SHELL`, plus these names. Names match case-insensitively. Vendor API keys are rejected at config load; `CLAUDE_*`, `CODEX_*`, and `GEMINI_*` names are never passed. |
| `verification_allowed_prefixes` | `[]` | Command prefixes a Work Packet Verification command must start with, matched word by word. A command that matches none, or that contains a shell control character, fails `verification_not_allowed` and routes to `planner`. Empty runs any command, with a startup and `doctor` `WARN`. See [Verification Command Allowlist](#verification-command-allowlist). |

To turn the gate off, set `enforce_work_packet: false` and say why:

```yaml
enforce_work_packet: false
work_packet_opt_out_reason: "Planner prompts do not write Work Packets yet."
```

The reason is stripped of surrounding whitespace. A config that sets
`enforce_work_packet: false` without a reason, or with a whitespace-only
reason, fails to load with a validation error that names both
`enforce_work_packet` and `work_packet_opt_out_reason`. A config that sets a
reason while the gate is on also fails to load, because the reason only applies
when the gate is off. `commit_mode: dispatcher` still needs the gate on: with
`commit_mode: dispatcher` and `enforce_work_packet: false`, the config fails
with the `commit_mode: dispatcher requires enforce_work_packet: true` error even
when a reason is set. When the gate is off, every dispatcher start logs a
`WARN` line with `enforce_work_packet: false` and the reason, and
`llm-handoff doctor` prints `WARN work-claim gate:` with it. When it is on,
the doctor prints `PASS work-claim gate:`. The doctor validates the current
handoff with the same Work Packet requirement as the dispatch loop, so a
planner assignment the dispatcher would reject for an unparseable Work Packet
also fails `llm-handoff doctor` while the gate is on.

When `enforce_work_packet` is `true`, the dispatcher verifies every
`status: ready_for_review` handoff from `backend` or `frontend` before it
dispatches the auditor. The checks are deterministic and run against the
`## Work Packet` the implementer received:

| Check | Error prefix | Passes when |
| --- | --- | --- |
| Work Packet | `work_packet_missing`, `work_packet_unparseable` | the assignment carries `Files in scope`, `Files out of bounds`, and `Verification command` |
| `scope_sha` | `scope_sha_not_head` | `scope_sha` resolves to the current HEAD; commits above it that touch only the handoff file are tolerated |
| Diff scope | `scope_violation` | every file changed since dispatch is inside `Files in scope` or is the handoff file, and none is under `Files out of bounds` |
| Working tree | `uncommitted_changes` | no new uncommitted changes exist outside the handoff file |
| Verification allowlist | `verification_not_allowed` | `verification_allowed_prefixes` is empty, or every `Verification command` starts with a listed prefix and contains no shell control character |
| Verification | `verification_failed` | the `Verification command` exits 0 within the timeout |
| Changes | `no_changes` | an implementer claim's commits since dispatch change at least one file other than the handoff file; applies in both commit modes |
| Evidence SHA | `evidence_sha_mismatch` | the `Commit SHA verified` value in `## Verification Evidence` names the same commit as `scope_sha` |

Path lists accept files, directories (`src/`), and globs, separated by commas,
semicolons, or whitespace. A value that uses backticks or an exception phrase is
read as prose: only backticked entries and bare words that look like paths
(containing `/`, `.`, or a glob character) count. In `Files out of bounds`,
entries after "other than", "except", "excluding", "apart from", or "but not"
are dropped up to the next comma or semicolon, so "every file in `src/` other
than `src/app.py`" puts `src/` out of bounds and leaves `src/app.py` alone.
When a file matches both lists, the narrowest entry decides: an exact file
beats a directory or glob, and a longer entry beats a shorter one. A tie goes
to `Files out of bounds`.

On failure the dispatcher does not dispatch the auditor. It rewrites
`HANDOFF.md` with `status: verified_fail`, `producer: validator`, a quoted
`reason` naming the failed checks, and a `## Dispatcher Verification` section
with the evidence, then routes back to the implementer. The bounce increments
the story's bounce count, so the three-fix circuit breaker still applies. A
missing or unusable Work Packet routes the bounce to `planner` instead, because
the packet is the planner's artifact. Every bounce carries the original
`## Work Packet` forward verbatim, and the gate reads the packet from the
assignment, so the implementer cannot change it. If the same Verification
command fails again as the only failed check, and no commit since the previous
bounce changed a file other than the handoff, the bounce goes to `planner` as
well. So does a Verification command that `verification_allowed_prefixes`
rejects (`verification_not_allowed`).

Every line of `Verification command` runs as its own command. A line that is
only a connective (`and`, `or`, `then`, `plus`, `also`) or ends in `:` is prose,
and the packet is `work_packet_unparseable`.

With the gate enabled, post-dispatch validation also rejects any handoff that
routes work to an implementer without a parseable Work Packet;
`work_packet_missing` and `work_packet_unparseable` become errors instead of
warnings, so the problem surfaces before an implementer session is spent.

The verification command is agent-written text. The dispatcher runs it through
the shell in `repo_root` with its own permissions. The implementer that wrote
the claim already ran in the same repository with provider permissions
disabled, so this adds no new exposure, but review the planner's Work Packet
the way you review any agent-written instruction. The command is bounded by
`work_packet_verification_timeout_seconds`; its process tree is killed on
timeout, and the command, exit code, and a bounded output tail are written to
the dispatch log.

The Verification command and every scanner run with an allowlisted
environment, not the dispatcher's own environment: the base and platform
variables listed for `work_claim_env_passthrough` above, plus the names that
key lists. Tokens such as `GITHUB_TOKEN` in the shell that started the
dispatcher do not reach them. A Verification command or scanner that needs
another variable, such as a proxy setting or a tool's cache directory, needs
that name listed:

```yaml
work_claim_env_passthrough: [HTTPS_PROXY, SSL_CERT_FILE]
```

Entries must be valid environment variable names (`^[A-Za-z_][A-Za-z0-9_]*$`)
after surrounding whitespace is stripped; an empty entry or a vendor API key
name such as `OPENAI_API_KEY` fails config load. This limits what the command
can read from the environment only. It still runs as your user account and can
use credentials stored outside the environment.

### Verification Command Allowlist

`verification_allowed_prefixes` limits which Work Packet Verification commands
the gate runs. It is optional but recommended:

```yaml
verification_allowed_prefixes: ["python -m pytest", "npm test"]
```

- Matching is by whole words. The command and each prefix are split on
  whitespace, and the command's leading words must equal a prefix's words,
  case-sensitively. `python -m pytest` allows `python -m pytest tests -q` and
  rejects `python -m pytestx` and `python -c "..."`.
- Once the list is set, a command containing any of `&`, `|`, `;`, `<`, `>`,
  `$`, backtick, `(`, `)`, `%`, `!`, a carriage return, or a newline is
  rejected even if its first words match, so an allowed prefix cannot be
  chained to a second command. `%` and `!` are included because on Windows
  the command runs through `cmd.exe`, which expands `%NAME%` (and `!NAME!`
  with delayed expansion) before parsing. Entries are stripped of surrounding whitespace; an empty entry or
  an entry containing one of these characters fails config load.
- The gate checks every Verification command in the packet before it runs
  any. If one is rejected, none runs, the claim fails
  `verification_not_allowed`, and the bounce routes to `planner` with
  `status: verified_fail`, because only the planner can change the Work
  Packet. Git state and scanner configuration failures still escalate to
  `user` first.
- With the list empty, any command runs, as before. Every dispatcher start
  then logs `WARN Verification allowlist: OFF`, and `llm-handoff doctor`
  prints `WARN verification allowlist:`. With the list set, the start logs
  the prefixes and the doctor prints `PASS verification allowlist:`. Neither
  line appears while the gate is off.
- `llm-handoff init --verification-command "<cmd>"` writes the confirmed
  command as the single entry. `init` rejects a command that contains one of
  the characters above.

Scanner commands are operator-authored config and are not prefix-checked. The
dispatcher logs the exact text of each scanner command and each Verification
command before it runs.

### Dispatcher Commits

```yaml
enforce_work_packet: true
commit_mode: dispatcher   # agent (default) | dispatcher
```

With `commit_mode: dispatcher`, implementers (`backend`, `frontend`) never
write to `.git`. The dispatcher adds an instruction to each implementer launch
telling it to leave changes uncommitted, leave `scope_sha` empty, and write
`pending-dispatcher-commit` as the evidence commit SHA. After a
`ready_for_review` handback, and before handoff validation, the dispatcher:

1. lists files changed since dispatch, skipping the handoff file and every file
   that was already dirty before dispatch;
2. stages only files inside the Work Packet's `Files in scope` and outside
   `Files out of bounds`;
3. commits exactly those paths with `git commit --no-verify --only`, under your
   Git identity, so other staged changes and repository hooks are not involved;
4. writes the new commit into the handoff as `scope_sha` and as the
   `Commit SHA verified` value.

Files it did not stage stay in the working tree and fail the gate's worktree
check, which sends the claim back to the implementer. As in agent commit mode,
the gate's `no_changes` check fails a claim whose commits since dispatch change
nothing but the handoff. This mode requires `enforce_work_packet: true`.

Files an implementer dispatch leaves uncommitted are the story's work, not
operator edits. The dispatcher remembers their content hashes, and the next
implementer dispatch for the same `story_id` (or, when the handoff has no
`story_id`, the same `## Work Packet` text, which a bounce carries forward
verbatim) treats any of them that are still unchanged as its own changes: the
commit step can stage them, and the worktree check fails the claim if they stay
uncommitted. A file edited in between, a different story, or a dispatcher
restart puts a file back in the "already dirty before dispatch" group, which is
never committed.

The one-shot handoff repair follows the same rule: in dispatcher mode its
prompt says to leave the handoff uncommitted, and if the repair commits the
handoff anyway, the dispatcher undoes that commit with `git reset --soft`, the
same way it does for the planner.

Use it when an implementer runs in a sandbox that keeps `.git` read-only (Codex
`workspace-write`), or simply to keep agents out of Git history.

### Publishing: Story Branches And Pull Requests

```yaml
commit_mode: dispatcher
publish:
  mode: local            # local (default) | merge-commit | pr
  base_branch: null      # default: the branch checked out when a story starts
  branch_prefix: llm-handoff/
  remote: origin         # pr mode
  auto_merge: true       # pr mode: merge once the audit passes
  wait_for_checks: false # pr mode: wait for the PR's CI checks before merging
  wait_for_reviewers: [] # pr mode + auto_merge: [codex] waits for Codex review
  review_timeout_seconds: 1200
  review_poll_seconds: 30
  max_review_rounds: 1 # >1 needs Codex to review every push on its own
  codex_bot_login: chatgpt-codex-connector[bot]
```

- `local`: commits stay on the branch you have checked out.
- `merge-commit`: before an implementer's first dispatch for a story, the
  dispatcher creates `llm-handoff/<story-id>-<title>` from the base branch.
  After the audit passes, it merges that branch into the base with `--no-ff`.
  The merge commit carries a report for people to read, built from data the
  loop already has. It has two sections, and the dispatcher lays it out
  itself, so the format holds whatever the agents wrote:
  - Summary: at most three sentences (about 500 characters) from the start
    of the Work Packet's Objective. A sentence that ends with a colon, such
    as "Acceptance checks:", starts the details instead.
  - Reviewer notes: `### Story` (story, epic, implementer), then
    `### Acceptance checks` (the rest of the Objective, with its lists and
    line breaks kept), then `### Audit` (both reviewers' verdicts, with the
    full reviews folded).

  The report is not part of the dispatcher's checks, and it leaves out what
  other places already show: changed files and commits (the pull request's
  Files and Commits tabs, or `git log`), the Work Packet and the gate's
  verification results (`HANDOFF.md`), and token usage (the dispatch log).
  Nothing is pushed.
- `pr`: same story branch. After the audit passes, the dispatcher pushes it,
  opens a pull request with that report as the body (`gh pr create`), and
  with `auto_merge: true` merges it (`gh pr merge --merge --delete-branch`)
  and continues on the updated base branch. It only auto-merges when every
  review thread on the pull request is resolved, including threads from
  people; otherwise the loop stops and leaves the pull request open. With
  `auto_merge: false` the loop stops after opening the pull request so you
  can review and merge it. With
  `wait_for_checks: true` it waits for the pull request's checks
  (`gh pr checks --watch`) before merging and stops if they fail; only enable
  this when the repository has CI.

#### Keeping The Story Branch Up To Date

The base branch can move while a story is in progress, for example when
another pull request merges. In `pr` mode, before every push of the story
branch (the first push and each correction round), the dispatcher runs
`git fetch <remote> <base_branch>` and merges `<remote>/<base_branch>` into the
story branch (`git merge --no-edit`). The review then sees the merged result.
If `gh pr merge` later reports that the pull request is not mergeable,
because the base branch moved again during the review, the dispatcher does
the same merge once more, pushes it, and retries the merge once.

When that merge conflicts, the dispatcher runs `git merge --abort` and pauses
(exit code 0). It names the conflicting files and keeps the story state in
`logs/dispatch/publish/story.json`. To resume, on the story branch run
`git merge <remote>/<base_branch>`, resolve the conflicts, and commit the
merge, then start the dispatcher again. At startup it pushes the story
branch and publishes it as usual. If it paused at the merge, your merge
commit lands on the open pull request. Codex reviews new pull requests, not
later pushes, and the dispatcher never asks for a review. So with
`wait_for_reviewers: [codex]` the dispatcher pushes that commit and pauses,
leaving the pull request open for you to review and merge; without it, the
dispatcher merges the pull request. If the retried
`gh pr merge` fails again, the dispatcher pauses the same way and leaves the
pull request open. The next start retries the merge, or you can merge it
yourself.

Many stories add a bullet at the top of the same changelog section, which
conflicts whenever two of them are open at once. A line in the target
repository's `.gitattributes` lets the local merge keep both sides:

```gitattributes
CHANGELOG.md merge=union
```

A union merge keeps every line from both sides without checking that the
result makes sense, and a blank line that both sides added can appear only
once, so only use it for files where that is safe. `init` does
not add it: the reference workflow does not ship a changelog, and
`.gitattributes` belongs to the target repository.

#### Waiting For The Codex Pull Request Review

With `wait_for_reviewers: [codex]`, `pr` mode with `auto_merge: true` waits
for the Codex code review (the ChatGPT Codex connector for GitHub) of the
exact commit it pushed before merging. Set this only when Codex reviews pull
requests in the repository on its own: the dispatcher never asks for a
review. The dispatcher polls the pull request with
`gh api` every `review_poll_seconds`. It counts only activity from
`codex_bot_login`, and only for the pushed commit's SHA:

- The review is finished when the Codex summary comment's `Code Review` row
  shows `Completed` for that commit, when Codex posts a pull request review
  on that commit, or when Codex adds a +1 reaction after the push.
- Inline review comments on that commit are findings.

The outcomes:

- No findings: the dispatcher merges the pull request as usual.
- Findings: the pull request stays open and is not merged. The dispatcher
  rewrites `HANDOFF.md` as a `verified_fail` bounce to the story's implementer.
  The bounce lists each finding (file, line, text, link) and carries the Work
  Packet. The implementer fixes the findings on the same story branch and
  writes a `## Review Responses` section with one `### Finding N` entry per
  finding: what it changed, or why it left the finding. The dispatcher saves
  that section when the gate passes, because the auditor rewrites
  `HANDOFF.md`. After the gate and audit pass again, the dispatcher pushes
  to the same pull request and updates its report. It posts each response as
  the reply on that finding's review thread and resolves the thread. A
  finding without a response keeps its thread unresolved, which blocks the
  merge. A response that starts with `Deferred:` (or `**Deferred:**`) marks
  a real finding left unfixed for later work, often because its fix is
  outside the Work Packet's Files in scope: the dispatcher posts the reply,
  logs a `WARN` naming the finding, and leaves the thread unresolved.
- Deferred findings only: when the only unresolved review threads are
  deferred findings, the dispatcher does not pause. It rewrites `HANDOFF.md`
  to route to `planner` with each deferred finding, the implementer's
  response, and the current Work Packet. The planner gets one pass per pull
  request and decides each finding:
  - Widen the scope: route back to the implementer with the same
    `story_id` and a Work Packet whose Files in scope cover the fix. The
    implementer answers under `## Review Responses` as usual, and the fix
    merges without another review once every thread is resolved.
  - Accept it as follow-up work: plan a follow-up story and add a
    `## Deferred Finding Decisions` section with a `### Finding N` entry that
    starts with `Follow-up:` and names it. After the planner's cycle passes
    validation, the dispatcher posts each decision as the reply, resolves
    the thread, and merges once every review thread is resolved.
  - Ask the user: route to `user`. The pull request stays open.

  The dispatcher pauses for a person when the planner leaves a finding
  neither accepted nor sent back, when the implementer defers a finding
  again after the planner's pass, or when any other review thread, such as
  a finding without a response or a person's comment, is still unresolved.
- One correction round by default (`max_review_rounds: 1`). The first review
  should surface what matters most; each review of a fix tends to find new
  edge cases in the new code, so the dispatcher does not wait for another
  review after the last correction round. It merges once every review thread
  is resolved. With a higher `max_review_rounds`, it waits for the review
  Codex runs on the pushed fix and can bounce again, up to that many rounds.
  That only works when Codex is set to review every push to a pull request,
  not just newly opened ones: the dispatcher never requests a review, so
  without per-push reviews the wait times out and the loop stops with the
  pull request left open.
- No finished review within `review_timeout_seconds`, a failed review, or a
  `gh` error: the loop stops and leaves the pull request open. It never
  merges without a finished review.

The pull request report has every `@name` mention neutralized (a word
joiner after `@`), because GitHub treats a mention of `@codex` anywhere in
the body, even in a code span, as a request for a Codex cloud task rather
than a code review.

Finding text comes from the pull request and is untrusted. The bounce tells
the implementer to check each finding and to fix only what holds up inside
the Work Packet's scope. The work-claim gate and the audit still apply to
the fix.

Agents never branch, push, or merge: the dispatcher does all of it, and only
after the work-claim gate and both reviewers pass. `pr` mode pushes with your
Git credentials and needs the GitHub CLI (`gh`) signed in. Both branch modes
require `commit_mode: dispatcher`. A failed fetch, push, merge, or `gh` call
stops the loop with the error instead of retrying. The exceptions are a
conflicting base branch merge, which pauses, and a pull request that is not
mergeable, which is retried once after a base branch merge.

### Scanners

The gate can also run operator-configured scanners before the verification
command. The list is empty by default, so nothing changes until you add
entries, and scanners only run while `enforce_work_packet` is `true`. Each
entry names a tool the target repository has installed; `llm-handoff` does not
bundle or install any of them.

```yaml
enforce_work_packet: true
scanners:
  - name: gitleaks
    command: gitleaks git --no-banner --redact --log-opts="{diff_range}" {repo_root}
    timeout_ms: 300000
    required: true
  - name: semgrep
    command: semgrep scan --config auto --error --quiet {changed_files}
    timeout_ms: 300000
    required: true
  - name: bandit
    command: bandit -q {changed_py_files}
    timeout_ms: 120000
    required: true
  - name: pip-audit
    command: pip-audit --strict
    timeout_ms: 300000
    required: false
```

On gitleaks releases before 8.19 use
`gitleaks detect --source {repo_root} --log-opts="{diff_range}"` instead of the
`git` subcommand. `semgrep --config auto` and `pip-audit` need network access.

Fields:

- `name`: label used in log lines and error prefixes; letters, digits, `.`,
  `_`, and `-`, unique within the config.
- `command`: shell text run in `repo_root` after placeholder substitution.
- `timeout_ms`: per-scanner bound, default `300000`; the process tree is
  killed on timeout.
- `required`: default `true`, so a missing executable fails the claim. With
  `false`, a missing executable is logged at WARN level and the scanner is
  skipped.

Placeholders:

| Placeholder | Value |
| --- | --- |
| `{diff_range}` | `<pre-dispatch HEAD>..<scope_sha>`, ready for `git log` or `git diff` options |
| `{changed_files}` | space-separated, shell-quoted repository-relative paths changed in that range that still exist |
| `{changed_py_files}` | the `.py` subset of `{changed_files}` |
| `{repo_root}` | the shell-quoted absolute repository root |

Scope gitleaks, semgrep, and bandit to the diff or the changed files as shown.
Run pip-audit unscoped, because a dependency audit is not diff-shaped. Literal
braces in a command must be doubled (`{{` and `}}`). A command that references
an unknown placeholder fails config load with `scanner_config_invalid:<name>`.
A scanner whose command references `{changed_files}` or `{changed_py_files}`
is skipped with an INFO log line when that list is empty for the claim.

Scanners run only after the `scope_sha`, diff-scope, and working-tree checks
pass; there is no value in scanning out-of-bounds work. They run in config
order before the verification command, and every scanner runs even after one
fails, so a single bounce reports every finding:

| Outcome | Error prefix | Effect |
| --- | --- | --- |
| non-zero exit | `scanner_failed:<name>` | claim bounces to the implementer with a bounded output tail |
| executable not found, `required: true` | `scanner_missing:<name>` | claim bounces to the implementer |
| executable not found, `required: false` | none | WARN log line, scanner skipped |
| timeout | `scanner_timeout:<name>` | process tree killed, claim bounces to the implementer |
| placeholder cannot be filled | `scanner_config_invalid:<name>` | rejected at config load; if it reaches runtime the claim escalates to `user` |

Scanner commands are operator-authored configuration, so they are trusted
operator input, unlike the Work Packet verification command, which is
agent-written. The dispatcher does not sandbox scanners any more than it
sandboxes agents. Scanners get the same allowlisted environment as the
Verification command; list any variable a scanner needs, such as a
`SEMGREP_APP_TOKEN` or a proxy setting, under `work_claim_env_passthrough`.

## Agent Environment

Each provider CLI is launched with an explicit environment built from an
allowlist. The dispatcher's own environment is not copied, so tokens such as
`GITHUB_TOKEN`, `AWS_*` credentials, or database passwords in the shell that
started the dispatcher do not reach the agents.

In the default `env_mode: allowlist`, a launch receives only:

- process basics: `PATH`, `HOME`, `USERPROFILE`, `SystemRoot`, `SystemDrive`,
  `TEMP`, `TMP`, `LANG`, `LC_ALL`, `TERM`, and the account name (`USERNAME`,
  `USER`, `LOGNAME`), which tools such as pytest use for per-user directories;
- non-secret process plumbing the CLIs need to start: `COMSPEC`, `PATHEXT`,
  `WINDIR`, `APPDATA`, `LOCALAPPDATA`, `TMPDIR`, `SHELL`;
- the provider's own configuration variables: `CLAUDE_*` for Claude Code,
  `CODEX_*` for Codex, and `GEMINI_*`, `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`, plus container sandbox
  variables (`DOCKER_HOST`, `DOCKER_CONTEXT`, `CONTAINER_HOST`,
  `SANDBOX_FLAGS`, `SEATBELT_PROFILE`) for Gemini CLI;
- any extra names listed under the role's `env_passthrough`.

Names are matched case-insensitively.

Vendor API keys follow a fixed rule in every mode. A key only reaches its own
provider, and only when the role opts in:

| Key | Passed to |
| --- | --- |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | never; Claude Code uses its own login |
| `OPENAI_API_KEY` | Codex roles with `use_api_key_env: true` |
| `GEMINI_API_KEY` | Gemini roles with `use_api_key_env: true` (or the planner flag above) |
| `GOOGLE_API_KEY` | never |

`env_passthrough` cannot name a vendor API key; config loading fails if it
does.

```yaml
agents:
  backend:
    provider: codex
    env_passthrough: [HTTPS_PROXY, SSL_CERT_FILE]
```

`env_mode: inherit` restores the previous behavior of copying the
dispatcher's environment into the launch. The vendor API key rule above still
applies. Use it only for debugging a CLI that needs a variable you have not
identified yet; prefer adding that name to `env_passthrough`.

Codex roles previously inherited `OPENAI_API_KEY`. A Codex setup that
authenticates with an API key instead of `codex login` now needs
`use_api_key_env: true` on that role.

## Launch Permissions And Sandboxing

Each role launches its provider CLI with the safer arguments below. A role can
restore the previous approval and sandbox bypass with `unsafe_bypass: true`.
The startup banner prints one `Launch <role>:` line per role and a
`WARN UNSAFE BYPASS:` line for every role that opts out.

| Provider | Default launch | `unsafe_bypass: true` |
| --- | --- | --- |
| Claude Code | `--permission-mode dontAsk --tools <tool names> --strict-mcp-config --allowedTools <allowed_tools> --disallowedTools WebFetch,WebSearch` | `--dangerously-skip-permissions` |
| Codex | `--sandbox workspace-write --ask-for-approval never` | `--yolo` |
| Gemini CLI | `--yolo --sandbox` | `--yolo --sandbox=false` |

Claude Code in `dontAsk` mode denies any tool call that `allowed_tools` does
not allow instead of prompting. `--allowedTools` only pre-approves rules, so
the launch also passes `--tools` with the base tool names from the list
(`Read`, `Bash`, and so on). Built-in tools outside the list are then not
available at all, even if user or project settings approve them.
`--strict-mcp-config` loads no MCP servers unless `allowed_tools` names an
`mcp__` tool. The default list is:

```yaml
allowed_tools:
  - Read
  - Edit
  - Write
  - Grep
  - Glob
  - Agent
  - Bash(git status:*)
  - Bash(git diff:*)
  - Bash(git log:*)
  - Bash(git show:*)
  - Bash(git cat-file:*)
  - Bash(git add:*)
  - Bash(git commit:*)
  - Bash(git rev-parse:*)
  - Bash(python:*)
  - Bash(python3:*)
  - Bash(pytest:*)
```

`Agent` is included because the Claude-side prompts delegate to the named
subagent, and `git cat-file` because the shipped `handoff-validator` resolves
SHAs with it. Setting `allowed_tools` on a role replaces the whole list. The
list is a guardrail, not a sandbox: `Bash(python:*)` still lets the agent run
arbitrary Python. `git push` is not on the list.

`llm-handoff init` reads the target repository's CI workflows,
`package.json`, `pyproject.toml`, `Makefile`, `Cargo.toml`, and `go.mod`,
and prints the build and test commands it finds, each with the file it came
from. It runs none of them and writes nothing about them. To confirm one, pass
`init --verification-command "<cmd>"`. The first word of the command is its
executable. It must be a bare program name matching
`^[A-Za-z0-9][A-Za-z0-9._+-]*$`: no path separators, quotes, or shell
operators, so `./run-tests.sh` and `a;b` are rejected and `init` writes no
files. `init` then gives every `provider: claude` role in the written
`dispatch_config.yaml` an `allowed_tools` list: the default list above plus
`Bash(<executable>:*)`, unless the default list already has that rule (for
example `python` or `pytest`). The rule allows every subcommand of that
executable, not only the confirmed command: `Bash(npm:*)` also allows
`npm install` and `npm publish`. Without `--verification-command`, `init`
writes the same config as before.

Codex keeps its `workspace-write` sandbox: writes are limited to the
workspace and network access is off by default. The sandbox always keeps
`.git` read-only, even when `.git` is listed under
`sandbox_workspace_write.writable_roots`, so a sandboxed role cannot commit on
its own. Use `commit_mode: dispatcher` for sandboxed Codex implementers: the
dispatcher commits the in-scope files after the role finishes. The reference
workflow ships `.codex/rules/llm-handoff.rules`, a Codex rules file (an
experimental Codex feature) that forbids `git push`. It does not let git
writes run outside the sandbox: rules match command prefixes only, so an
allow rule for `git commit` would also allow `git commit -a` or a trailing
`--amend`. Everything the role runs stays sandboxed.

Codex applies the strictest matching rule, but a project rule cannot override
an `allow` rule in `~/.codex/rules/` that matches the full shell command (for
example a remembered `pwsh -Command "git push origin main"` approval). Review
your user-level rules before relying on the push ban.

On Windows the sandbox runs commands as a separate local account. Read-only git
commands may need `-c safe.directory=<repository root>`, and pytest's per-user
temp directory is not usable from the sandbox; the work-claim gate re-runs the
Verification command outside the sandbox. If Codex still cannot start or
commit in your setup, check its sandbox settings before reaching for
`unsafe_bypass`.

Gemini CLI keeps `--yolo` so tool calls do not prompt, but runs inside its
container sandbox. That needs Docker or Podman on `PATH` (macOS can use
`sandbox-exec`; `GEMINI_SANDBOX` selects a runtime). When no runtime is found
the dispatcher refuses to start and names the key to set, for example
`agents.planner.unsafe_bypass: true`. The adapter repeats the same check
before every Gemini launch. Inside the container, the role sees the mounted
project directory; host tools and credentials outside it may be missing.

`permissions_flag` now only applies together with `unsafe_bypass: true`. It
replaces `--dangerously-skip-permissions` for that role. Setting it without
`unsafe_bypass` fails at config load.

These flags were checked against the provider CLI documentation, not run end
to end against every CLI version on every platform. If a CLI rejects a flag,
pin `unsafe_bypass: true` on that role only while you investigate, and treat
the run as unsandboxed.

## Protected Control Files

Default:

```yaml
protected_paths:
  - AGENTS.md
  - docs/handoff/HANDBOOK.md
  - docs/handoff/*_PROMPT.md
  - docs/handoff/README.md
  - dispatch_config.yaml
  - .claude/**
  - .codex/**
  - .gemini/**
  - .githooks/**
```

Patterns are relative to the repository root. `*` matches within one path
segment and `**` matches any depth. Missing paths are ignored.

Before each dispatch the dispatcher hashes the matching files. After the
dispatch, a protected file that changed on disk or in the new commits, and
that the role's Work Packet did not list under `Files in scope`, stops the
loop: the handoff is rewritten to route to `user` with a
`protected_file_modified:` error and the changed paths. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#protected-control-files).

Set `protected_paths: []` to turn the check off. A config file passed with
`--config` under another name is not protected unless you add its path. If a
provider CLI writes its own state under one of these directories in your
setup, narrow the pattern rather than removing the check.

## Supervised Dispatch

The optional supervisor wrapper is configured through command-line options, not
through `dispatch_config.yaml`. It still uses this file to resolve the normal
dispatcher settings and, when enabled, the configured `agents.<role>` entry for
`--repair-role`.

Repair is off by default. Setting `--repair-role validator`, for example,
invokes the existing `validator` agent config for one bounded repair attempt
unless `--max-repair-attempts` is set explicitly.

See [docs/SUPERVISED_DISPATCH.md](docs/SUPERVISED_DISPATCH.md) for the
supervisor options, run-state files, incident bundles, heartbeat behavior, lock
semantics, notification hook, and shell wrapper commands.

## Agent Roles

Public docs and templates should use generic role names:

- `planner`
- `backend`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider names are adapter details. The runtime dispatch layer selects the
adapter from each role's `provider` field. The currently registered runtime
adapters are `codex`, `gemini`, and `claude`; providers without a registered
adapter fail closed at config load.

When a config file defines only some `agents` entries, the loader merges those
entries over the reference defaults. After merging, all reference roles must be
present. If a role changes providers, omitted provider-specific fields are
filled from that provider's default field bundle rather than the role's old
provider defaults.

| Role | Reference Provider |
| --- | --- |
| `planner` | `gemini` |
| `backend` | `codex` |
| `frontend` | `gemini` or manual frontend pause |
| `auditor` | `claude` |
| `validator` | `claude` |
| `finalizer` | `claude` |

The table documents the shipped reference workflow defaults, not a hard
runtime matrix. If you map a role to another registered provider, make sure the
target repository has the corresponding provider prompt, skill, subagent, or
agent file needed for that provider to perform the role.

Example:

```yaml
agents:
  backend:
    provider: codex
    binary: codex
    skill_name: llm-handoff
    resume: true
```

### All-Claude Mapping

The reference workflow ships a Claude subagent for every role, so a machine
with only Claude Code installed can run the whole loop:

| Role | Claude subagent |
| --- | --- |
| `planner` | `.claude/agents/planner.md` |
| `backend` | `.claude/agents/backend.md` |
| `frontend` | `.claude/agents/frontend.md` |
| `auditor` | `.claude/agents/auditor.md` |
| `validator` | `.claude/agents/handoff-validator.md` |
| `finalizer` | `.claude/agents/ledger-updater.md` |

`llm-handoff init --provider claude` writes this mapping into
`dispatch_config.yaml`, with every role on `provider: claude` and
`audit.security_provider: claude`. Without `--provider`, `init` writes it
automatically when `claude` is the only provider CLI on `PATH`, and keeps the
reference mapping otherwise. `init` prints the mapping it chose and the
provider CLIs it found.

With every role on Claude, the security reviewer runs on the same vendor as
the implementer, so it is a second read-only review rather than an
independent one, and each dispatcher start logs a warning saying so.

### All-Codex Mapping

The reference workflow ships a Codex repo skill for every role, so a machine
with only the Codex CLI installed can run the whole loop. Each role names its
skill in `skill_name`, and the Codex adapter tells Codex to use that skill:

| Role | Codex skill |
| --- | --- |
| `planner` | `.codex/skills/llm-handoff-planner/SKILL.md` |
| `backend` | `.codex/skills/llm-handoff/SKILL.md` |
| `frontend` | `.codex/skills/llm-handoff-frontend/SKILL.md` |
| `auditor` | `.codex/skills/llm-handoff-auditor/SKILL.md` |
| `validator` | `.codex/skills/llm-handoff-validator/SKILL.md` |
| `finalizer` | `.codex/skills/llm-handoff-finalizer/SKILL.md` |

The validator skill covers both the handoff validation and handoff routing
tasks; the dispatcher's prompt names which one applies.

`llm-handoff init --provider codex` writes this mapping into
`dispatch_config.yaml`, with every role on `provider: codex`,
`audit.security_provider: codex`, and `normalizer.provider: codex` (the
normalizer runs through the Codex CLI login, so no API key is needed).
Without `--provider`, `init` writes it automatically when `codex` is the only
provider CLI on `PATH`. If both `claude` and `codex` are on `PATH`, `init`
keeps the reference mapping; pass `--provider` to choose one.
`--verification-command` changes no `agents:` entry under this mapping,
because it only adds `allowed_tools` to Claude roles.

With every role on Codex, the security reviewer runs on the same vendor as
the implementer, so it is a second read-only review rather than an
independent one, and the dispatcher logs a warning saying so.

Whether Codex's `workspace-write` sandbox lets a role run `git add` and
`git commit` is unverified. The skills tell a sandboxed role not to work
around the sandbox. For the planner, backend, and frontend roles,
`commit_mode: dispatcher` (see [Dispatcher Commits](#dispatcher-commits))
avoids the question: the dispatcher commits backend and frontend work, and the
planner writes nothing to `.git` because its handoff is left uncommitted on
purpose. It does not cover the finalizer: the dispatcher does not
commit the finalizer's `PROJECT_STATE.md` and `HANDOFF.md` update. If the
sandbox blocks the finalizer's commit, the finalizer reports no commit SHA,
the dispatcher pauses, and you commit those files yourself.

A fresh `codex exec` run returns the role's final message as a
`{status, summary}` JSON envelope. For the `validator` and `finalizer`
roles, the dispatcher parses the `summary` text, so the validator's `VALID:`
report and the finalizer's `PROJECT STATE UPDATED` report must be the whole
`summary`. Output that is not an envelope, such as a resumed session's
answer, is parsed as it is.

### Fallback Agents

A role can name one fallback agent. When the primary agent fails fatally, the
dispatcher runs the same dispatch once more on the fallback, in the same cycle:

```yaml
agents:
  backend:
    provider: codex
    fallback:
      provider: claude
      model: claude-sonnet-5-5
  auditor:
    provider: claude
    model: claude-opus-5-5
    fallback:
      provider: claude
      model: claude-sonnet-5-5
```

A failure is fatal, and fails over, only when the primary could not do the
work at all:

| Reason | Detected from |
| --- | --- |
| Launch failure | No output and an OS "file not found" or "command not found" error. |
| Sign-in failure | The same markers that stop the loop without a fallback. |
| Rate or usage limit | 429, `RESOURCE_EXHAUSTED`, `rate_limit_error`, `overloaded_error`, quota, or usage-limit messages in stderr or the end of stdout. |
| Timeout | The process ran past the role's `timeout_ms`, or its optional `idle_timeout_ms`, and was killed. |

Any other non-zero exit is the agent's own failure: the fallback does not run
and the failure counts toward `max_consecutive_failures` as before.

The dispatcher also skips the fallback when the primary failed after it:

- rewrote `HANDOFF.md`, because running the role again could repeat a
  finished handoff; or
- changed a file under `protected_paths`, because the fallback would read the
  changed file as instructions. The protected-file gate then handles the
  change as usual.

In both cases the run counts as a normal failure, exactly as it would without
a fallback. The next cycle reads the new handoff unless that failure stops the
loop, for example a sign-in failure or `max_consecutive_failures` reached.

Rules and limits:

- The fallback block takes the same fields as a role. Omitted fields come from
  the fallback provider's defaults, not from the primary.
- The fallback must launch a different provider, a different `binary`, or,
  for `claude`, a different `model`. The Codex and Gemini adapters do not pass
  a model flag, so a second model on those providers needs a different binary
  or account.
- One level only: a fallback cannot have its own fallback.
- The fallback starts a fresh session. When it runs, the dispatcher also drops
  the planner's saved session, so the next planner dispatch starts fresh.
- The fallback provider needs the same role files the primary needs, such as
  the Claude subagent, Codex skill, or Gemini agent for that role.
- If the fallback also fails, its result is the dispatch result and the
  normal failure count applies. A sign-in failure on the fallback still stops
  the loop.
- By default a hung agent is only detected when `timeout_ms` expires. Set
  `idle_timeout_ms` (see [Idle Output Timeout](#idle-output-timeout)) or lower
  `timeout_ms` on a role to fail over sooner. Gemini retries its own 429s first (`retries`, with
  backoff); set `retries: 0` on a Gemini role to fail over at the first 429.
- Fallbacks cover role dispatches. In audit fan-out the quality reviewer's
  prompt and verdict parsing depend on its provider, so it fails over only to
  an `auditor` fallback on the same provider, with `audit.review_timeout_ms`.
  The security reviewer and the next-agent normalizer do not use fallbacks.

Startup logs a `Launch <role> fallback:` line for each fallback, and a Gemini
fallback needs a sandbox runtime like any Gemini role.

### Idle Output Timeout

`idle_timeout_ms` is an optional per-role limit on silence. When it is set, the
dispatcher kills the agent's process tree once the process has written nothing
to stdout or stderr for that long, instead of waiting for the full
`timeout_ms`:

```yaml
agents:
  backend:
    provider: codex
    timeout_ms: 1200000
    idle_timeout_ms: 300000
    fallback:
      provider: claude
      model: claude-sonnet-5-5
```

The kill is reported as `Process timed out after <n> seconds without output
(idle_timeout_ms).`, which counts as a timeout, so a configured fallback runs
next. Without a fallback it is an ordinary timed-out dispatch.

It is off by default (unset), and it should stay off unless you know the CLI
streams steadily. Some CLIs write nothing for long stretches while they work:
Gemini in plain-text mode (the fallback when `stream-json` is not supported)
and Claude in buffered text mode print only when they finish, and any CLI can
go quiet during a long model call or a slow tool. Set the limit well above the
longest quiet stretch you have seen in that role's dispatch logs. A fallback
block takes its own `idle_timeout_ms`; it does not inherit the primary's.
The audit security reviewer and the next-agent normalizer do not use it.

## Provider Fields

Common fields:

- `provider`: adapter family such as `codex`, `gemini`, or `claude`.
- `binary`: command name or absolute executable path.
- `agent_name`: optional display/subagent name for logs or provider-specific
  role selection.
- `resume`: whether the adapter may reuse a prior session.
- `timeout_ms`: process timeout for a single dispatch.
- `idle_timeout_ms`: optional limit on how long the process may write no
  output before it is killed. Off by default. See
  [Idle Output Timeout](#idle-output-timeout).
- `retries`: provider retry count where supported.
- `env_mode`: `allowlist` (default) or `inherit`. See
  [Agent Environment](#agent-environment).
- `env_passthrough`: extra environment variable names to pass to this role.
- `use_api_key_env`: pass the provider's own API key (`OPENAI_API_KEY` for
  Codex, `GEMINI_API_KEY` for Gemini). Ignored for Claude.
- `unsafe_bypass`: restore the provider's previous approval and sandbox bypass
  flags for this role. Off by default and logged as a startup warning.
- `fallback`: optional agent that reruns the dispatch after a fatal failure.
  See [Fallback Agents](#fallback-agents).

Codex-specific fields:

- `skill_name`: Codex skill to invoke in the target repo.
- `use_api_key_env`: whether to pass `OPENAI_API_KEY`. Off by default.

Claude-specific fields:

- `model`: Claude model for subagent or validator calls.
- `allowed_tools`: tool rules passed to `--allowedTools` in the default
  `dontAsk` launch. See [Launch Permissions And Sandboxing](#launch-permissions-and-sandboxing).
- `permissions_flag`: bypass flag used only with `unsafe_bypass: true`.

Gemini-specific fields:

- `mention`: optional agent mention used by Gemini agent files.
- `use_api_key_env`: whether to preserve `GEMINI_API_KEY` for a Gemini-backed
  role. `GOOGLE_API_KEY` is always stripped by the current adapter.

## Audit Fan-Out

The auditor route runs a read-only security reviewer next to the quality
reviewer by default. Turning it off is an explicit opt-out that needs a
written reason.

```yaml
audit:
  reviewers: [quality, security]  # the default
  security_provider: null       # claude | gemini | codex; null = auto
  block_severity: high          # critical | high | medium | low
  review_timeout_ms: 900000
```

| Key | Default | Meaning |
| --- | --- | --- |
| `audit.reviewers` | `[quality, security]` | Reviewers on the auditor route. Must include `quality`. Supported values are `quality` and `security`. Leaving out `security` needs `audit.security_opt_out_reason`. |
| `audit.security_opt_out_reason` | unset | Required, non-empty text when `audit.reviewers` leaves out `security`; rejected while `security` is listed. |
| `audit.security_provider` | auto | CLI used for the security reviewer. When unset, see the automatic choice below. |
| `audit.block_severity` | `high` | A finding at or above this severity routes the story back to the implementer. |
| `audit.review_timeout_ms` | `900000` | Process timeout for each reviewer in fan-out mode. |

To opt out of the security reviewer:

```yaml
audit:
  reviewers: [quality]
  security_opt_out_reason: "Local prototype; no secrets or user data."
```

A config that leaves out `security` without a reason fails to load with a
validation error that names `security_opt_out_reason`. When security is
opted out, every dispatcher start logs a `WARN` line with the reason, and
`llm-handoff doctor` prints `WARN security review:` with it. The auditor
route then uses the single-auditor path, which writes `HANDOFF.md` itself.

With `audit.security_provider` unset, the dispatcher picks the security
reviewer for each implementer role (`backend` and `frontend`) in this order:

1. the first of `claude`, `codex`, `gemini` whose CLI is on `PATH` and that
   differs from the implementer role's provider;
2. otherwise the implementer's own vendor when its CLI is on `PATH`, with a
   `WARN` that the review is not independent;
3. otherwise `run` refuses to start with an error that begins
   `security_reviewer_failed:`.

The CLI lookup is the one `llm-handoff doctor` uses for role binaries, and the
binary is the one configured for the first role that uses that provider.

At startup, before any agent launches, `run` checks the security reviewer for
each implementer role, whether configured or chosen automatically: its CLI
must be on `PATH`, and a Gemini reviewer also needs a sandbox runtime (unless
the Gemini role sets `unsafe_bypass: true`) and the Gemini CLI security
extension. A failed check stops the run with a `security_reviewer_failed:`
error. `--dry-run` skips this check.

With `security` enabled, the auditor route works differently:

- The dispatcher records `git rev-parse HEAD` before each backend or frontend
  dispatch. The review scope is that commit through the handoff's `scope_sha`.
  If the pre-dispatch commit is unknown, for example after a dispatcher
  restart, the scope falls back to `scope_sha~1..scope_sha`.
- The quality reviewer (the configured `auditor` agent) and the security
  reviewer run concurrently on that range. Both are told not to write
  `HANDOFF.md` and must return a JSON verdict.
- The dispatcher, not a reviewer, rewrites `HANDOFF.md`. Any finding at or
  above `block_severity`, or a `fail` verdict from either reviewer, produces
  `status: verified_fail` routed back to the implementer with a
  `## Security Findings` section. Otherwise the handoff routes where the
  quality reviewer directed, with sub-threshold findings under
  `## Security Notes`.
- `verified_fail` results increment the story's bounce count, so the
  three-fix circuit breaker still applies.
- Each reviewer's raw stdout and stderr, exit code, elapsed time, and parsed
  verdict are written under `logs/dispatch/audit/<timestamp>-cycle<N>/`. The
  timestamp prefix keeps a restarted dispatcher from overwriting earlier
  cycles.

Security reviewer invocation by provider:

- `claude`: `claude -p` in `dontAsk` permission mode with an allow-list of
  read and `git diff`/`git show`/`git log` tools, edit tools denied, and
  `--json-schema` structured output. The prompt asks for Claude Code's
  built-in security review restricted to the given range.
- `gemini`: `gemini -p "/security:analyze ..."` in `--approval-mode plan`
  with `--sandbox`, so it needs Docker or Podman like the Gemini roles. Plan
  mode is not enough by itself: headless Gemini CLI auto-approves
  `exit_plan_mode` and then continues in YOLO mode. When Gemini is the
  security reviewer, `run` refuses to start when no sandbox runtime is found,
  instead of failing at the first audit.
  The target repository's Gemini CLI must have the security extension
  installed:

  ```bash
  gemini extensions install https://github.com/gemini-cli-extensions/security
  ```

  The dispatcher checks `gemini extensions list` at startup and again before
  each review, and fails closed with `security_extension_missing:` when the
  extension is absent or listed as disabled for the repository (a cross
  marker, or `Enabled (Workspace): false`). Enable a disabled extension with
  `gemini extensions enable gemini-cli-security`. The extension
  documents its analyze command as interactive-first, so headless results
  depend on the installed extension version.
- `codex`: when `codex-security` is on `PATH` and `scope_sha` is the
  checked-out `HEAD`, the dispatcher runs `codex-security scan . --diff <base>
  --json`. Otherwise it runs `codex exec --sandbox read-only --json
  --output-schema <schema>` with a security-review prompt. Codex Security was
  in beta as of mid-2026 and may require plan-level or approved access; its
  JSON output shape is parsed defensively, and an unrecognized severity is
  treated as `high`.

Security reviewers use the environment allowlist of the role that maps to
their provider, and keep their read-only flags even if that role sets
`unsafe_bypass: true`.

A missing, malformed, or timed-out reviewer verdict, a security reviewer CLI
that is no longer installed, and any reviewer that changes `HANDOFF.md`,
fails the audit dispatch. The dispatcher restores the
pre-review handoff and counts the cycle as a dispatch failure.

## Next-Agent Normalizer

The dispatcher has an internal next-agent normalizer for non-canonical
`next_agent` values. The deterministic enum check runs first. If the value is
not a supported role, the dispatcher may make a small model call to normalize
obvious freeform values to one of the public roles.

The normalizer is intentionally constrained:

- it can only return a canonical role such as `backend`, `frontend`, or
  `auditor`;
- it should return `unknown` when intent is ambiguous;
- `unknown` fails closed and routes to validation or user intervention instead
  of guessing.

Config shape:

```yaml
normalizer:
  provider: claude
  model: claude-haiku-5-5
  timeout_ms: 60000
  on_unknown: fail_closed
```

The registered normalizer adapters are `claude`, `codex`, `gemini`, and
`openai`. `claude` is the reference default. If `provider` changes and `model`
is omitted, the loader fills a provider-specific default model.

The normalizer has two runtime auth shapes:

- API key path: the configured provider API is called with the
  `NormalizedNextAgent` Pydantic schema. Gemini requires `GEMINI_API_KEY` or
  `GOOGLE_API_KEY`; OpenAI requires `OPENAI_API_KEY`; Claude uses
  `ANTHROPIC_API_KEY` when present.
- CLI auth path: Claude may fall back to the local Claude Code CLI session when
  no Anthropic API key is available. The `codex` adapter always uses the local
  Codex CLI login. Gemini and OpenAI normalizer adapters are API-only in the
  current scaffold.

Choose `provider: codex` when the machine has the Codex CLI signed in (for
example with a ChatGPT account) but no `OPENAI_API_KEY`, such as an all-Codex
setup:

```yaml
normalizer:
  provider: codex
  # model defaults to gpt-5.4-mini; set it to a model your Codex login can use.
```

The `codex` adapter runs `codex exec --sandbox read-only --skip-git-repo-check`
with the arguments passed as a list and no shell. The prompt is sent on stdin.
It never reads or passes `OPENAI_API_KEY`, even when that variable is set; use
`provider: openai` for API-key auth. The normalizer needs no repository
access, and the read-only sandbox still permits file reads, so the run starts
from a private temporary directory outside the repository instead of the
checkout. That directory also holds the JSON schema and last-message files.
Project-level `.codex/` settings in the target repository therefore do not
apply to the normalizer. The sandbox does not block reads by absolute path, so
this limits, but does not remove, what an injected `next_agent` value could
ask Codex to read. `timeout_ms` bounds the whole process tree: on a timeout
the dispatcher terminates Codex and its descendants, including the process
behind a Windows `.cmd` shim. A missing `codex` binary, a launch error, a
timeout, a nonzero exit, or output that is not a valid role fails closed and
leaves the handoff unchanged.

`llm-handoff doctor` prints one `normalizer` line. It fails when the
configured normalizer cannot run: for `codex`, the `codex` binary is not
found; for `claude`, neither `ANTHROPIC_API_KEY` nor the `claude` binary is
available; for `openai`, `OPENAI_API_KEY` is not set; for `gemini`, neither
`GEMINI_API_KEY` nor `GOOGLE_API_KEY` is set. The doctor never calls the
provider, so a passing line reports auth as not verified.

The API path should not silently fall back to CLI auth after selecting an API
key. If the structured API call fails, the dispatcher should fail closed and
leave the handoff unchanged for validation.

There is intentionally no `enabled` switch. The normalizer is part of the
routing pipeline: exact enum matches remain deterministic, obvious freeform
values get one constrained normalization attempt, and ambiguity fails closed.

## Prompt Templates

Prompt templates should be stored in the source tree and overrideable from the
target repo.

Planned config:

```yaml
prompts_dir: .llm-handoff/prompts
```

If omitted, source-tree defaults should be used.

## Frontmatter Contract

A handoff begins with YAML frontmatter:

```markdown
---
next_agent: planner
reason: "Scope the first implementation task."
producer: user
---
```

Expected keys:

- `next_agent`: required role name.
- `reason`: required short routing reason.
- `producer`: role or actor that wrote the handoff.
- `scope_sha`: optional commit SHA related to the completed work.
- `prior_sha`: optional prior handoff or work commit.
- `close_type`: optional workflow close marker.

The dispatcher should fail closed on invalid or ambiguous frontmatter.
