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

## Provider Fields

Common fields:

- `provider`: adapter family such as `codex`, `gemini`, or `claude`.
- `binary`: command name or absolute executable path.
- `agent_name`: optional display/subagent name for logs or provider-specific
  role selection.
- `resume`: whether the adapter may reuse a prior session.
- `timeout_ms`: process timeout for a single dispatch.
- `retries`: provider retry count where supported.

Codex-specific fields:

- `skill_name`: Codex skill to invoke in the target repo.

Claude-specific fields:

- `model`: Claude model for subagent or validator calls.
- `permissions_flag`: optional CLI permission flag.

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
- `gemini`: `gemini -p "/security:analyze ..."` in `--approval-mode plan`.
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
