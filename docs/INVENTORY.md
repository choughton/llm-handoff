# File Inventory

Every Markdown document, prompt file, provider agent template, and config
artifact that ships in this repository, with a one-line purpose. This page is
a reference index. For the user-facing path, start at the
[README](../README.md).

Three copies of the handoff protocol exist on purpose:

- `docs/handoff/` is this repository's own protocol, used when the dispatcher
  runs against itself.
- `examples/reference-workflow/` is the template that `llm-handoff init`
  copies into a target repository. It is the authoritative inventory of what
  `init` writes.
- `llm_handoff/prompt_templates/linear/` is the Linear-mode prompt text that
  `init --tracker linear` renders into the target repository's `AGENTS.md` and
  `docs/handoff/HANDBOOK.md`.

## Contents

- [User Guides](#user-guides)
- [Design And Contributor Docs](#design-and-contributor-docs)
- [Runtime And Packaging Files](#runtime-and-packaging-files)
- [Handoff Protocol (This Repository)](#handoff-protocol-this-repository)
- [Provider Agent Templates (This Repository)](#provider-agent-templates-this-repository)
- [Linear-Mode Prompt Templates](#linear-mode-prompt-templates)
- [Reference Workflow Template](#reference-workflow-template)
- [CLI Commands](#cli-commands)
- [Test Fixture Handoffs](#test-fixture-handoffs)

## User Guides

| File | Purpose |
| --- | --- |
| [README.md](../README.md) | Front door: quick start, mode switching, gotchas, and how the loop works. |
| [QUICKSTART.md](../QUICKSTART.md) | The README quick start's five steps as a command sheet, with Windows PowerShell and macOS/Linux variants. |
| [WALKTHROUGH.md](../WALKTHROUGH.md) | Slower first-run guide: the two-repo model, what `init` copies, common pauses. |
| [INSTALL.md](../INSTALL.md) | Requirements, install paths, provider CLI checks, `doctor`, and debugging. |
| [CONFIGURATION.md](../CONFIGURATION.md) | Every supported `dispatch_config.yaml` key, including the work-claim gate, scanners, publishing, sandboxing, Linear mode, and audit fan-out. |
| [docs/SUPERVISED_DISPATCH.md](SUPERVISED_DISPATCH.md) | Optional supervisor wrapper: heartbeats, run-state file, incident bundles, bounded repair role. |

## Design And Contributor Docs

| File | Purpose |
| --- | --- |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | Module map, design principles, and the provider-native subagent boundary. |
| [docs/TESTING.md](TESTING.md) | Test strategy and how to run the suite. |
| [AGENTS.md](../AGENTS.md) | Instructions for coding agents working in this repository. |
| [PROJECT_STATE.md](../PROJECT_STATE.md) | This repository's own durable project state when it dispatches against itself. |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Contribution scope and project boundaries. |
| [SECURITY.md](../SECURITY.md) | Threat model, safe usage, and vulnerability reporting. |
| [CHANGELOG.md](../CHANGELOG.md) | Release and extraction history. |
| [.github/pull_request_template.md](../.github/pull_request_template.md) | Pull request template: summary, changes, reviewer notes, test plan. |
| [docs/INVENTORY.md](INVENTORY.md) | This page. |

## Runtime And Packaging Files

| File | Purpose |
| --- | --- |
| [dispatch_config.example.yaml](../dispatch_config.example.yaml) | Fully commented example config. Shows every optional block, including scanners and the `tracker` block. |
| [pyproject.toml](../pyproject.toml) | Package metadata, runtime dependencies, and the `llm-handoff` console command. |
| [requirements-dev.txt](../requirements-dev.txt) | Source-checkout runtime and test dependencies. |
| [.pre-commit-config.yaml](../.pre-commit-config.yaml) | Contributor hooks for whitespace, YAML/TOML, large files, and Ruff. |
| [llm_handoff/rationalization_patterns.yaml](../llm_handoff/rationalization_patterns.yaml) | Per-role regex patterns the validator reports as advisory warnings when a handoff body contradicts its claimed status. |
| [llm_handoff/schemas/codex_final_response.schema.json](../llm_handoff/schemas/codex_final_response.schema.json) | JSON schema passed to `codex exec --output-schema`; a Codex role's final message must match it. |
| [LICENSE](../LICENSE) | Apache License 2.0. |

## Handoff Protocol (This Repository)

The protocol files under `docs/handoff/`. Every role reads `HANDBOOK.md` and
`SHARED_REPO_INIT_PROMPT.md`, then its own `*_HANDOFF_PROMPT.md`.

| File | Role | Purpose |
| --- | --- | --- |
| [docs/handoff/README.md](handoff/README.md) | all | Handoff directory guide: required frontmatter, status enum, evidence block, Work Packet format. |
| [docs/handoff/HANDOFF.md](handoff/HANDOFF.md) | shared | The live state file. Agents read and overwrite it. |
| [docs/handoff/HANDBOOK.md](handoff/HANDBOOK.md) | all | Shared operating rules: schema, status enum, evidence, escalation, failure modes. |
| [docs/handoff/SHARED_REPO_INIT_PROMPT.md](handoff/SHARED_REPO_INIT_PROMPT.md) | all | Common bootstrap instructions for a fresh session. |
| [docs/handoff/PLANNER_INITIAL_PROMPT.md](handoff/PLANNER_INITIAL_PROMPT.md) | planner | First planner session for a new workflow. |
| [docs/handoff/PLANNER_HANDOFF_PROMPT.md](handoff/PLANNER_HANDOFF_PROMPT.md) | planner | Ongoing planner dispatch: scope the next story, write the Work Packet. |
| [docs/handoff/TPM_HANDOFF_PROMPT.md](handoff/TPM_HANDOFF_PROMPT.md) | tpm | Optional story picker when `agents.tpm` is configured: assign the next `remaining_stories` entry with a Work Packet, escalate to the planner. |
| [docs/handoff/BACKEND_HANDOFF_PROMPT.md](handoff/BACKEND_HANDOFF_PROMPT.md) | backend | Backend implementation prompt. |
| [docs/handoff/FRONTEND_HANDOFF_PROMPT.md](handoff/FRONTEND_HANDOFF_PROMPT.md) | frontend | Frontend implementation prompt. |
| [docs/handoff/AUDITOR_HANDOFF_PROMPT.md](handoff/AUDITOR_HANDOFF_PROMPT.md) | auditor | Adversarial review of a `ready_for_review` claim. |
| [docs/handoff/FINALIZER_HANDOFF_PROMPT.md](handoff/FINALIZER_HANDOFF_PROMPT.md) | finalizer | Project-state update after an epic-level approval. |

## Provider Agent Templates (This Repository)

Provider-native agent files, skills, and policies. These are adapter-side
helpers for the roles above, not additional dispatcher roles. Only the active
dispatcher role writes `HANDOFF.md`; see
[ARCHITECTURE.md](ARCHITECTURE.md#provider-native-subagents).

### Claude Code

| File | Purpose |
| --- | --- |
| [.claude/agents/planner.md](../.claude/agents/planner.md) | Planner subagent (all-Claude mapping). |
| [.claude/agents/tpm.md](../.claude/agents/tpm.md) | Tpm subagent: picks the next story when `agents.tpm` maps to claude. |
| [.claude/agents/backend.md](../.claude/agents/backend.md) | Backend subagent (all-Claude mapping). |
| [.claude/agents/frontend.md](../.claude/agents/frontend.md) | Frontend subagent (all-Claude mapping). |
| [.claude/agents/auditor.md](../.claude/agents/auditor.md) | Auditor subagent. |
| [.claude/agents/handoff-validator.md](../.claude/agents/handoff-validator.md) | Validator subagent: routing, SHA metadata, pointer consistency. |
| [.claude/agents/handoff-router.md](../.claude/agents/handoff-router.md) | Routing clarification subagent for ambiguous `next_agent` values. |
| [.claude/agents/ledger-updater.md](../.claude/agents/ledger-updater.md) | Finalizer subagent (file name kept for compatibility). |

### Codex

| File | Purpose |
| --- | --- |
| [.codex/skills/llm-handoff/SKILL.md](../.codex/skills/llm-handoff/SKILL.md) | Backend skill (the reference mapping's Codex role). |
| [.codex/skills/llm-handoff/agents/openai.yaml](../.codex/skills/llm-handoff/agents/openai.yaml) | Codex agent metadata for the backend skill. |
| [.codex/skills/llm-handoff-planner/SKILL.md](../.codex/skills/llm-handoff-planner/SKILL.md) | Planner skill (all-Codex mapping). |
| [.codex/skills/llm-handoff-tpm/SKILL.md](../.codex/skills/llm-handoff-tpm/SKILL.md) | Optional tpm skill (`skill_name: llm-handoff-tpm`). |
| [.codex/skills/llm-handoff-frontend/SKILL.md](../.codex/skills/llm-handoff-frontend/SKILL.md) | Frontend skill (all-Codex mapping). |
| [.codex/skills/llm-handoff-auditor/SKILL.md](../.codex/skills/llm-handoff-auditor/SKILL.md) | Auditor skill (all-Codex mapping). |
| [.codex/skills/llm-handoff-validator/SKILL.md](../.codex/skills/llm-handoff-validator/SKILL.md) | Validator and router skill (all-Codex mapping). |
| [.codex/skills/llm-handoff-finalizer/SKILL.md](../.codex/skills/llm-handoff-finalizer/SKILL.md) | Finalizer skill (all-Codex mapping). |
| [.codex/rules/llm-handoff.rules](../.codex/rules/llm-handoff.rules) | Codex prefix rules: `git push` and other git writes forbidden; use `commit_mode: dispatcher` for commits. |

### Antigravity

| File | Purpose |
| --- | --- |
| [.agents/agents/planner.md](../.agents/agents/planner.md) | Planner agent template. |
| [.agents/agents/tpm.md](../.agents/agents/tpm.md) | Optional tpm agent template (`agy --agent tpm`). |
| [.agents/agents/backend.md](../.agents/agents/backend.md) | Backend agent template. |
| [.agents/agents/frontend.md](../.agents/agents/frontend.md) | Frontend agent template. |
| [.agents/agents/auditor.md](../.agents/agents/auditor.md) | Auditor agent template. |
| [.agents/agents/handoff-validator.md](../.agents/agents/handoff-validator.md) | Validator agent template. |
| [.agents/agents/handoff-router.md](../.agents/agents/handoff-router.md) | Routing clarification agent template. |
| [.agents/agents/ledger-updater.md](../.agents/agents/ledger-updater.md) | Finalizer agent template. |
| [.agents/agents/supervisor-repair.md](../.agents/agents/supervisor-repair.md) | Supervised dispatcher repair agent template. |

## Linear-Mode Prompt Templates

Package-internal Markdown that `llm-handoff init --tracker linear` renders
into the target repository between `<!-- llm-handoff:linear:begin -->` and
`<!-- llm-handoff:linear:end -->` markers. The text names settings by their
`tracker.*` keys and embeds no values. See
[Tracker (Optional Linear Mode)](../CONFIGURATION.md#tracker-optional-linear-mode).

| File | Rendered into | Purpose |
| --- | --- | --- |
| [llm_handoff/prompt_templates/linear/AGENTS.md](../llm_handoff/prompt_templates/linear/AGENTS.md) | `AGENTS.md` | Shared Linear-mode rules: the issue holds the work, `HANDOFF.md` stays the routing lease, issue text is untrusted data. |
| [llm_handoff/prompt_templates/linear/EVIDENCE_COMMENT.md](../llm_handoff/prompt_templates/linear/EVIDENCE_COMMENT.md) | `docs/handoff/HANDBOOK.md` | YAML evidence-comment contract (`lease_opened`, `implementation_handoff`, `audit_passed`, and so on). |
| [llm_handoff/prompt_templates/linear/ISSUE_TEMPLATE.md](../llm_handoff/prompt_templates/linear/ISSUE_TEMPLATE.md) | `docs/handoff/HANDBOOK.md` | Linear issue template whose `## Work Packet` uses the labels the gate parses. |
| [llm_handoff/prompt_templates/linear/roles/planner.md](../llm_handoff/prompt_templates/linear/roles/planner.md) | `docs/handoff/HANDBOOK.md` | Issue selection rules, lease fields, user-attention steps. |
| [llm_handoff/prompt_templates/linear/roles/planner_with_tpm.md](../llm_handoff/prompt_templates/linear/roles/planner_with_tpm.md) | `docs/handoff/HANDBOOK.md` | Planner section when a `tpm` is configured: the tpm leases issues, the planner applies the tpm selection rules only when it rebuilds a missing lease, user-attention steps. |
| [llm_handoff/prompt_templates/linear/roles/tpm.md](../llm_handoff/prompt_templates/linear/roles/tpm.md) | `docs/handoff/HANDBOOK.md` | Rendered only when a `tpm` is configured. Issue selection rules, lease fields, user-attention steps, lease hygiene. |
| [llm_handoff/prompt_templates/linear/roles/backend.md](../llm_handoff/prompt_templates/linear/roles/backend.md) | `docs/handoff/HANDBOOK.md` | Backend evidence comment on handback. |
| [llm_handoff/prompt_templates/linear/roles/frontend.md](../llm_handoff/prompt_templates/linear/roles/frontend.md) | `docs/handoff/HANDBOOK.md` | Frontend evidence comment on handback. |
| [llm_handoff/prompt_templates/linear/roles/auditor.md](../llm_handoff/prompt_templates/linear/roles/auditor.md) | `docs/handoff/HANDBOOK.md` | Pass and fail issue-state transitions, user-attention steps. |
| [llm_handoff/prompt_templates/linear/roles/validator.md](../llm_handoff/prompt_templates/linear/roles/validator.md) | `docs/handoff/HANDBOOK.md` | Read-only check that the latest evidence comment matches `HANDOFF.md`. |
| [llm_handoff/prompt_templates/linear/roles/finalizer.md](../llm_handoff/prompt_templates/linear/roles/finalizer.md) | `docs/handoff/HANDBOOK.md` | Finalizer is read-only in Linear mode: no comments, no issue updates. |

## Reference Workflow Template

Everything under `examples/reference-workflow/` except its README is copied
into a target repository by `llm-handoff init`. The template README is the
guide to that directory.

| File | Purpose |
| --- | --- |
| [examples/reference-workflow/README.md](../examples/reference-workflow/README.md) | Template directory guide: included files, guardrails, role and handoff contracts. Not copied. |
| [examples/reference-workflow/AGENTS.md](../examples/reference-workflow/AGENTS.md) | Target-repo agent instructions. Carries a managed `llm-handoff` block. |
| [examples/reference-workflow/PROJECT_STATE.md](../examples/reference-workflow/PROJECT_STATE.md) | Target-repo durable state: active epic, blockers, completed-scope ledger. |
| [examples/reference-workflow/dispatch_config.yaml](../examples/reference-workflow/dispatch_config.yaml) | Target-repo config. Ships with `scanners: []` and the default gates on. |
| [examples/reference-workflow/.gitignore](../examples/reference-workflow/.gitignore) | Managed block ignoring `logs/dispatch/` and `.dispatch.lock`. |

<details>
<summary>Template handoff prompts</summary>

| File | Purpose |
| --- | --- |
| [docs/handoff/README.md](../examples/reference-workflow/docs/handoff/README.md) | Template handoff directory guide. |
| [docs/handoff/HANDOFF.md](../examples/reference-workflow/docs/handoff/HANDOFF.md) | Starter handoff routed to `planner`. |
| [docs/handoff/HANDBOOK.md](../examples/reference-workflow/docs/handoff/HANDBOOK.md) | Shared handbook. Linear mode appends its sections here. |
| [docs/handoff/SHARED_REPO_INIT_PROMPT.md](../examples/reference-workflow/docs/handoff/SHARED_REPO_INIT_PROMPT.md) | Shared startup prompt. |
| [docs/handoff/PLANNER_INITIAL_PROMPT.md](../examples/reference-workflow/docs/handoff/PLANNER_INITIAL_PROMPT.md) | First planner prompt. |
| [docs/handoff/PLANNER_HANDOFF_PROMPT.md](../examples/reference-workflow/docs/handoff/PLANNER_HANDOFF_PROMPT.md) | Ongoing planner prompt. |
| [docs/handoff/TPM_HANDOFF_PROMPT.md](../examples/reference-workflow/docs/handoff/TPM_HANDOFF_PROMPT.md) | Optional tpm prompt. |
| [docs/handoff/BACKEND_HANDOFF_PROMPT.md](../examples/reference-workflow/docs/handoff/BACKEND_HANDOFF_PROMPT.md) | Backend prompt. |
| [docs/handoff/FRONTEND_HANDOFF_PROMPT.md](../examples/reference-workflow/docs/handoff/FRONTEND_HANDOFF_PROMPT.md) | Frontend prompt. |
| [docs/handoff/AUDITOR_HANDOFF_PROMPT.md](../examples/reference-workflow/docs/handoff/AUDITOR_HANDOFF_PROMPT.md) | Auditor prompt. |
| [docs/handoff/FINALIZER_HANDOFF_PROMPT.md](../examples/reference-workflow/docs/handoff/FINALIZER_HANDOFF_PROMPT.md) | Finalizer prompt. |

</details>

<details>
<summary>Template provider agent files, skills, and guardrails</summary>

| File | Purpose |
| --- | --- |
| [.claude/agents/planner.md](../examples/reference-workflow/.claude/agents/planner.md) | Claude planner subagent. |
| [.claude/agents/tpm.md](../examples/reference-workflow/.claude/agents/tpm.md) | Claude tpm subagent. |
| [.claude/agents/backend.md](../examples/reference-workflow/.claude/agents/backend.md) | Claude backend subagent. |
| [.claude/agents/frontend.md](../examples/reference-workflow/.claude/agents/frontend.md) | Claude frontend subagent. |
| [.claude/agents/auditor.md](../examples/reference-workflow/.claude/agents/auditor.md) | Claude auditor subagent. |
| [.claude/agents/handoff-validator.md](../examples/reference-workflow/.claude/agents/handoff-validator.md) | Claude validator subagent. |
| [.claude/agents/handoff-router.md](../examples/reference-workflow/.claude/agents/handoff-router.md) | Claude router subagent. |
| [.claude/agents/ledger-updater.md](../examples/reference-workflow/.claude/agents/ledger-updater.md) | Claude finalizer subagent. |
| [.claude/settings.json](../examples/reference-workflow/.claude/settings.json) | Claude Code deny rules for push, network fetch, and destructive commands, plus the `PreToolUse` hook. |
| [.claude/hooks/block_dangerous_commands.py](../examples/reference-workflow/.claude/hooks/block_dangerous_commands.py) | Standard-library hook that blocks dangerous shell commands in every permission mode. |
| [.codex/skills/llm-handoff/SKILL.md](../examples/reference-workflow/.codex/skills/llm-handoff/SKILL.md) | Codex backend skill. |
| [.codex/skills/llm-handoff/agents/openai.yaml](../examples/reference-workflow/.codex/skills/llm-handoff/agents/openai.yaml) | Codex agent metadata. |
| [.codex/skills/llm-handoff-planner/SKILL.md](../examples/reference-workflow/.codex/skills/llm-handoff-planner/SKILL.md) | Codex planner skill. |
| [.codex/skills/llm-handoff-tpm/SKILL.md](../examples/reference-workflow/.codex/skills/llm-handoff-tpm/SKILL.md) | Codex tpm skill (optional role). |
| [.codex/skills/llm-handoff-frontend/SKILL.md](../examples/reference-workflow/.codex/skills/llm-handoff-frontend/SKILL.md) | Codex frontend skill. |
| [.codex/skills/llm-handoff-auditor/SKILL.md](../examples/reference-workflow/.codex/skills/llm-handoff-auditor/SKILL.md) | Codex auditor skill. |
| [.codex/skills/llm-handoff-validator/SKILL.md](../examples/reference-workflow/.codex/skills/llm-handoff-validator/SKILL.md) | Codex validator and router skill. |
| [.codex/skills/llm-handoff-finalizer/SKILL.md](../examples/reference-workflow/.codex/skills/llm-handoff-finalizer/SKILL.md) | Codex finalizer skill. |
| [.codex/rules/llm-handoff.rules](../examples/reference-workflow/.codex/rules/llm-handoff.rules) | Codex rules file. |
| [.agents/agents/planner.md](../examples/reference-workflow/.agents/agents/planner.md) | Antigravity planner agent. |
| [.agents/agents/tpm.md](../examples/reference-workflow/.agents/agents/tpm.md) | Antigravity tpm agent (optional role). |
| [.agents/agents/backend.md](../examples/reference-workflow/.agents/agents/backend.md) | Antigravity backend agent. |
| [.agents/agents/frontend.md](../examples/reference-workflow/.agents/agents/frontend.md) | Antigravity frontend agent. |
| [.agents/agents/auditor.md](../examples/reference-workflow/.agents/agents/auditor.md) | Antigravity auditor agent. |
| [.agents/agents/handoff-validator.md](../examples/reference-workflow/.agents/agents/handoff-validator.md) | Antigravity validator agent. |
| [.agents/agents/handoff-router.md](../examples/reference-workflow/.agents/agents/handoff-router.md) | Antigravity router agent. |
| [.agents/agents/ledger-updater.md](../examples/reference-workflow/.agents/agents/ledger-updater.md) | Antigravity finalizer agent. |
| [.agents/agents/supervisor-repair.md](../examples/reference-workflow/.agents/agents/supervisor-repair.md) | Antigravity supervised repair agent. |

</details>

## CLI Commands

`llm-handoff` and `python -m llm_handoff` run the same CLI. Run any command
with `--help` for the full option list.

| Command | Purpose |
| --- | --- |
| `llm-handoff [--repo-root P] [--config P] [--dry-run]` | Run the dispatch loop. `--dry-run` reports the route and provider without launching an agent. |
| `llm-handoff init TARGET [--dry-run] [--force] [--provider claude\|codex] [--tracker linear ...]` | Copy the reference workflow into a target repository. |
| `llm-handoff doctor [--repo-root P] [--config P] [--probe-tracker ISSUE]` | Preflight: Git, provider CLIs, sandbox runtime, gates, scanners, handoff file, Linear connectors. Exit `1` on any error. |
| `llm-handoff stop [--repo-root P]` | Ask a running dispatcher to finish its current cycle and exit. |
| `python -m llm_handoff.supervisor ...` | Optional supervised wrapper. See [SUPERVISED_DISPATCH.md](SUPERVISED_DISPATCH.md). |

Other run flags: `--manual-frontend`, `--no-interactive-pause`,
`--no-backend-resume`, `--no-planner-resume`.
See [Runtime Controls](../CONFIGURATION.md#runtime-controls).

## Test Fixture Handoffs

Parser and router fixtures under `tests/ported/fixtures/handoffs/`. They are
test inputs, not user-facing documents, and several deliberately exercise
legacy or malformed shapes.

<details>
<summary>Fixture list</summary>

| File | Exercises |
| --- | --- |
| [audit_story_close_next_story.md](../tests/ported/fixtures/handoffs/audit_story_close_next_story.md) | Story close routed to the next story. |
| [conflicting_signals.md](../tests/ported/fixtures/handoffs/conflicting_signals.md) | Frontmatter and body disagree. |
| [empty.md](../tests/ported/fixtures/handoffs/empty.md) | Empty file. |
| [escalation.md](../tests/ported/fixtures/handoffs/escalation.md) | Route to `user`. |
| [legacy_backend_handback_story_close.md](../tests/ported/fixtures/handoffs/legacy_backend_handback_story_close.md) | Legacy backend handback shape. |
| [legacy_manual_frontend_dispatch.md](../tests/ported/fixtures/handoffs/legacy_manual_frontend_dispatch.md) | Legacy manual frontend dispatch. |
| [misroute.md](../tests/ported/fixtures/handoffs/misroute.md) | Legacy prose route to `validator` for misroute clarification. |
| [next_step_header_epic_close.md](../tests/ported/fixtures/handoffs/next_step_header_epic_close.md) | Auditor epic close with a `Next Step For auditor` self-route header. |
| [next_step_header_for_backend.md](../tests/ported/fixtures/handoffs/next_step_header_for_backend.md) | Backend route signalled in a header. |
| [next_step_qualifier_suffix_auditor.md](../tests/ported/fixtures/handoffs/next_step_qualifier_suffix_auditor.md) | Auditor route with a qualifier suffix. |
| [next_step_subheading_frontend.md](../tests/ported/fixtures/handoffs/next_step_subheading_frontend.md) | Frontend route in a subheading. |
| [no_signal.md](../tests/ported/fixtures/handoffs/no_signal.md) | No routing signal at all. |
| [planner_task_assignment_backend.md](../tests/ported/fixtures/handoffs/planner_task_assignment_backend.md) | Planner assigns backend work. |
| [prose_next_agent_planner_legacy_alias.md](../tests/ported/fixtures/handoffs/prose_next_agent_planner_legacy_alias.md) | Legacy alias for `planner` in prose. |
| [story_close_ledger_language.md](../tests/ported/fixtures/handoffs/story_close_ledger_language.md) | Story close using ledger wording. |

</details>
