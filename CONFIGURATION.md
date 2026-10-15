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
    model: claude-opus-4-7
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

Off by default:

```yaml
enforce_work_packet: false
work_packet_verification_timeout_seconds: 600
```

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
| Verification | `verification_failed` | the `Verification command` exits 0 within the timeout |

On failure the dispatcher does not dispatch the auditor. It rewrites
`HANDOFF.md` with `status: verified_fail`, `producer: validator`, a quoted
`reason` naming the failed checks, and a `## Dispatcher Verification` section
with the evidence, then routes back to the implementer. The bounce increments
the story's bounce count, so the three-fix circuit breaker still applies. A
missing or unusable Work Packet routes the bounce to `planner` instead, because
the packet is the planner's artifact. Every bounce carries the original
`## Work Packet` forward verbatim.

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
check, which sends the claim back to the implementer. The gate also fails a
claim whose commits since dispatch change nothing but the handoff
(`no_changes`). This mode requires `enforce_work_packet: true`.

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
```

- `local`: commits stay on the branch you have checked out.
- `merge-commit`: before an implementer's first dispatch for a story, the
  dispatcher creates `llm-handoff/<story-id>-<title>` from the base branch.
  After the audit passes, it merges that branch into the base with `--no-ff`.
  The merge commit carries a report built from data the loop already has: the
  Work Packet, the passing `## Dispatcher Verification` result, both reviewers'
  verdicts, and the story's `usage:` lines with the reported cost. Nothing is
  pushed.
- `pr`: same story branch. After the audit passes, the dispatcher pushes it,
  opens a pull request with that report as the body (`gh pr create`), and
  with `auto_merge: true` merges it (`gh pr merge --merge --delete-branch`)
  and continues on the updated base branch. With `auto_merge: false` the loop
  stops after opening the pull request so you can review and merge it. With
  `wait_for_checks: true` it waits for the pull request's checks
  (`gh pr checks --watch`) before merging and stops if they fail; only enable
  this when the repository has CI.

Agents never branch, push, or merge: the dispatcher does all of it, and only
after the work-claim gate and both reviewers pass. `pr` mode pushes with your
Git credentials and needs the GitHub CLI (`gh`) signed in. Both branch modes
require `commit_mode: dispatcher`. A failed push, merge, or `gh` call stops the
loop with the error instead of retrying.

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
sandboxes agents.
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

With every role on Claude, an enabled security reviewer runs on the same
vendor as the implementer, so it is a second read-only review rather than an
independent one.

## Provider Fields

Common fields:

- `provider`: adapter family such as `codex`, `gemini`, or `claude`.
- `binary`: command name or absolute executable path.
- `agent_name`: optional display/subagent name for logs or provider-specific
  role selection.
- `resume`: whether the adapter may reuse a prior session.
- `timeout_ms`: process timeout for a single dispatch.
- `retries`: provider retry count where supported.
- `env_mode`: `allowlist` (default) or `inherit`. See
  [Agent Environment](#agent-environment).
- `env_passthrough`: extra environment variable names to pass to this role.
- `use_api_key_env`: pass the provider's own API key (`OPENAI_API_KEY` for
  Codex, `GEMINI_API_KEY` for Gemini). Ignored for Claude.
- `unsafe_bypass`: restore the provider's previous approval and sandbox bypass
  flags for this role. Off by default and logged as a startup warning.

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

The auditor route can optionally run a security reviewer next to the normal
quality reviewer. The feature is off by default; the defaults below keep the
single-auditor path exactly as it was.

```yaml
audit:
  reviewers: [quality]          # add `security` to enable the fan-out
  security_provider: null       # claude | gemini | codex; null = auto
  block_severity: high          # critical | high | medium | low
  review_timeout_ms: 900000
```

| Key | Default | Meaning |
| --- | --- | --- |
| `audit.reviewers` | `[quality]` | Reviewers on the auditor route. Must include `quality`. Supported values are `quality` and `security`. |
| `audit.security_provider` | auto | CLI used for the security reviewer. When unset, the first of `claude`, `codex`, `gemini` that differs from the implementer role's provider. |
| `audit.block_severity` | `high` | A finding at or above this severity routes the story back to the implementer. |
| `audit.review_timeout_ms` | `900000` | Process timeout for each reviewer in fan-out mode. |

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
  `exit_plan_mode` and then continues in YOLO mode. With
  `audit.security_provider: gemini`, `run` refuses to start when no sandbox
  runtime is found, instead of failing at the first audit.
  The target repository's Gemini CLI must have the security extension
  installed:

  ```bash
  gemini extensions install https://github.com/gemini-cli-extensions/security
  ```

  The dispatcher checks `gemini extensions list` first and fails closed with
  `security_extension_missing:` when the extension is absent. The extension
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

A missing, malformed, or timed-out reviewer verdict, and any reviewer that
changes `HANDOFF.md`, fails the audit dispatch. The dispatcher restores the
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
  model: claude-haiku-4-5
  timeout_ms: 60000
  on_unknown: fail_closed
```

The registered normalizer adapters are `claude`, `gemini`, and `openai`.
`claude` is the reference default. If `provider` changes and `model` is omitted,
the loader fills a provider-specific default model.

The normalizer has two runtime auth shapes:

- API key path: the configured provider API is called with the
  `NormalizedNextAgent` Pydantic schema. Gemini requires `GEMINI_API_KEY` or
  `GOOGLE_API_KEY`; OpenAI requires `OPENAI_API_KEY`; Claude uses
  `ANTHROPIC_API_KEY` when present.
- CLI auth path: Claude may fall back to the local Claude Code CLI session when
  no Anthropic API key is available. Gemini and OpenAI normalizer adapters are
  API-only in the current scaffold.

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
