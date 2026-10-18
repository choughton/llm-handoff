# Reference Workflow

This directory contains the copyable written protocol for a target repository
that wants to use `llm-handoff`.

The dispatcher is not just Python code. It depends on agent instructions,
handoff conventions, ignore rules, and a config file that all agree on the same
role names.

## Included Files

`llm-handoff init` copies every file in this directory except this README:

```text
AGENTS.md
PROJECT_STATE.md
dispatch_config.yaml
.gitignore
.geminiignore
docs/handoff/README.md
docs/handoff/HANDOFF.md
docs/handoff/HANDBOOK.md
docs/handoff/SHARED_REPO_INIT_PROMPT.md
docs/handoff/PLANNER_INITIAL_PROMPT.md
docs/handoff/PLANNER_HANDOFF_PROMPT.md
docs/handoff/BACKEND_HANDOFF_PROMPT.md
docs/handoff/FRONTEND_HANDOFF_PROMPT.md
docs/handoff/AUDITOR_HANDOFF_PROMPT.md
docs/handoff/FINALIZER_HANDOFF_PROMPT.md
.claude/agents/planner.md
.claude/agents/backend.md
.claude/agents/frontend.md
.claude/agents/auditor.md
.claude/agents/handoff-validator.md
.claude/agents/handoff-router.md
.claude/agents/ledger-updater.md
.claude/settings.json
.claude/hooks/block_dangerous_commands.py
.codex/skills/llm-handoff/SKILL.md
.codex/skills/llm-handoff/agents/openai.yaml
.codex/skills/llm-handoff-planner/SKILL.md
.codex/skills/llm-handoff-frontend/SKILL.md
.codex/skills/llm-handoff-auditor/SKILL.md
.codex/skills/llm-handoff-validator/SKILL.md
.codex/skills/llm-handoff-finalizer/SKILL.md
.codex/rules/llm-handoff.rules
.gemini/agents/planner.md
.gemini/agents/backend.md
.gemini/agents/frontend.md
.gemini/agents/auditor.md
.gemini/agents/validator.md
.gemini/agents/finalizer.md
.gemini/policies/planner_guardrails.toml
.agents/agents/planner.md
.agents/agents/backend.md
.agents/agents/frontend.md
.agents/agents/auditor.md
.agents/agents/handoff-validator.md
.agents/agents/handoff-router.md
.agents/agents/ledger-updater.md
.agents/agents/supervisor-repair.md
```

`AGENTS.md`, `.gitignore`, `.geminiignore`, and `.claude/settings.json` are
merged into existing target files; the rest are copied.

## Guardrails

The reference workflow ships provider guardrails that deny `git push`, `curl`,
`wget`, `rm -rf`, and `git reset --hard`:

- `.claude/settings.json` adds Claude Code deny rules for those commands and
  for `WebFetch`/`WebSearch`, plus a `PreToolUse` hook. The hook,
  `.claude/hooks/block_dangerous_commands.py`, is a standard-library Python
  script. It inspects every Bash or PowerShell call, including chained
  commands and `bash -c` wrappers, and blocks matches with exit code 2. It
  runs in every permission mode, including roles with `unsafe_bypass: true`.
  The hook command calls `python`; change it to `python3` on systems without
  a `python` executable.
- `.gemini/policies/planner_guardrails.toml` holds the same deny rules in
  Gemini CLI's policy engine schema. Gemini CLI's workspace policy tier is
  currently disabled, so the copy inside the project has no effect. Copy it
  to `~/.gemini/policies/` (Windows: `%USERPROFILE%\.gemini\policies\`) to
  enforce it. User-tier rules apply to every Gemini CLI session on that
  account.

These rules match command prefixes. They catch mistakes and simple prompt
injection; they do not stop an agent that writes a script to do the same
thing. Keep push credentials out of the dispatch environment. See the threat
model in the `llm-handoff` SECURITY.md.

Both files sit under protected paths, so a role that edits them without
listing them in its Work Packet stops the dispatch loop.

## Role Contract

Public role names:

- `planner`
- `backend`
- `frontend`
- `auditor`
- `validator`
- `finalizer`
- `user`

Provider CLIs are configured separately. For example, the shipped
`dispatch_config.yaml` maps `planner` and `frontend` to Antigravity (`agy`)
and `backend` to Codex without making those provider names part of the
handoff protocol. `llm-handoff init --tracker linear` writes `planner` and
`frontend` on Claude instead, because Linear mode does not support
`antigravity` roles.

## Handoff Contract

A handoff starts with YAML frontmatter:

```markdown
---
next_agent: planner
reason: Scope the first implementation task.
producer: user
---
```

The body should be clear enough for the next role to act without relying on
hidden state.

## Default Path

Recommended:

```text
docs/handoff/HANDOFF.md
```

Supported:

```text
HANDOFF.md
```

The nested path is recommended for non-trivial repos because it leaves room for
handoff examples, archive files, and helper prompts.

## Usage Notes

From the `llm-handoff` source checkout, initialize a target repository with:

```bash
python -m llm_handoff init path/to/your-project --template reference-workflow --dry-run
python -m llm_handoff init path/to/your-project --template reference-workflow
```

The initializer copies this directory's contents except this README. It skips
identical files and refuses to overwrite changed files unless `--force` is
passed.

After copying, edit `dispatch_config.yaml`, `PROJECT_STATE.md`, and
`docs/handoff/HANDOFF.md` for the target repo's first assignment.
