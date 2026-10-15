# Security

`llm-handoff` shells out to external agent CLIs and reads prompt files from the
target repository. Treat those files as executable workflow instructions.

## Supported Versions

No public release is supported yet. This repository is in pre-release
extraction.

After the first tagged release, supported versions will be listed here.

## Safe Usage

- Do not run the dispatcher against an untrusted repository without reviewing
  its handoff, prompt, and agent instruction files.
- Do not put secrets in `HANDOFF.md`, `PROJECT_STATE.md`, prompt templates, or
  dispatch logs.
- `HANDOFF.md` and `PROJECT_STATE.md` are intended to be committed workflow
  state. In team repositories, treat them like any other committed project
  context: review them for sensitive paths, customer details, credentials, and
  private implementation notes before publishing or sharing.
- Review provider CLI permissions before enabling a workflow.
- On Windows, prefer explicit, trusted CLI install paths when overriding provider
  commands. The dispatcher may execute `.cmd` wrappers found on `PATH`; an
  untrusted repository or shell environment can make wrapper resolution unsafe.
- The optional audit security reviewer is an LLM review, not a security
  guarantee. It is not hardened against prompt injection in repository
  content. See the Audit Fan-Out section of `docs/ARCHITECTURE.md`.
- `auto_push` is reserved and not read by the current dispatcher, which never
  pushes. Agents can still push if a push credential is reachable from the
  dispatch environment; see [Threat Model](#threat-model).
- Treat generated handoff instructions as untrusted until validated.
- The work-claim gate (`enforce_work_packet`) is on by default. It makes the
  dispatcher itself run the planner's Work Packet `Verification command`
  through the shell in the target repository. The agents already run there
  with provider permissions disabled, so this adds no new exposure, but it is
  still execution of agent-written text. Review the handoff files of a
  repository before you run the dispatcher there. Turning the gate off needs
  `enforce_work_packet: false` plus a non-empty `work_packet_opt_out_reason`,
  which every dispatcher start and `llm-handoff doctor` report as a warning.
- `scanners` entries in `dispatch_config.yaml` are operator-authored commands,
  so they are trusted operator input rather than agent-written text. The
  dispatcher does not sandbox scanners any more than it sandboxes agents; a
  scanner runs with the dispatcher's permissions in the target repository.
  Review the config the same way you review any script the repository runs.
- If `.dispatch.lock` exists, confirm no other dispatcher is active before
  removing it manually.

## Threat Model

The dispatcher runs agent CLIs that read files from the target repository and
act on them with shell access. Treat every agent as able to do anything its
launch mode allows, and treat the repository content it reads as untrusted
input.

### Prompt injection through repository content

Agents read `HANDOFF.md`, prompt templates, source files, test output,
dependency metadata, and anything else in the repository. Text in any of
these can carry instructions. `HANDOFF.md` is written by one agent and read
by the next, so a confused or injected role can try to steer the next one.

What limits it:

- the deterministic validator decides whether a handoff advances, not the
  agent's own claims;
- each CLI launches in its restricted mode by default (Claude Code
  `dontAsk` with a tool allowlist and web tools denied, Codex
  `workspace-write`, Gemini CLI in its container sandbox);
- the reference workflow ships deny rules and a `PreToolUse` hook for
  `git push`, `curl`, `wget`, `rm -rf`, and `git reset --hard`.

What it does not stop: an agent that is allowed to run `python` or edit
source can still run arbitrary code inside its sandbox. Command deny rules
match prefixes and can be bypassed through aliases, scripts, or interpreters.
They catch mistakes and simple injections, not a determined attacker.

### Credential inheritance

Each provider CLI gets an allowlisted environment, so `GITHUB_TOKEN`, cloud
credentials, and other secrets in the dispatcher's shell are not passed on.
Vendor API keys only reach their own provider, and only when a role opts in.

This does not cover credentials that live outside environment variables. The
agents run as your user account. A Git credential manager, an SSH agent, an
SSH key in your home directory, or a cloud CLI login can all be used by any
process the agent starts, unless the provider's sandbox blocks it.
`env_mode: inherit` and `unsafe_bypass: true` remove the corresponding
protections for that role.

### Self-modifying control files

`AGENTS.md`, the handoff prompts, the provider agent directories, and the
dispatch config tell each role what to do. A role that edits them can change
the next role's instructions. The dispatcher hashes these files before every
dispatch and routes the handoff to `user` with a `protected_file_modified:`
error when a role changes one without listing it in its Work Packet. See
`protected_paths` in [CONFIGURATION.md](CONFIGURATION.md#protected-control-files).

The check covers files inside the repository. Provider configuration in your
home directory, such as user-level Gemini policies or Claude Code settings,
is outside it. The provider sandboxes are what keep agents away from those
files.

### Recommended isolation

Run dispatch inside WSL2, a dev container, or a VM that holds a clone of the
repository, the provider CLI logins it needs, and no push credential: no SSH
key with write access, no credential helper token, and no `GITHUB_TOKEN`.
Review the resulting commits and push from your host. This is the only setup
in which a push prohibition does not depend on prompts, deny rules, or the
agent's cooperation.

On Windows, this also matches the provider sandboxes: Claude Code's Bash
sandbox needs WSL2, the Codex native Windows sandbox is experimental, and
Gemini CLI's sandbox needs Docker or Podman.

## Reporting A Vulnerability

Use GitHub private vulnerability reporting if it is enabled for this repository.
If it is not enabled, contact the maintainer through GitHub before posting
security details publicly.

Please include:

- affected version or commit SHA;
- operating system;
- provider CLI involved, if any;
- minimal reproduction steps;
- whether secrets, repository writes, or remote pushes are involved.

## Security Boundaries

The dispatcher does not sandbox provider CLIs itself. By default it launches
each CLI in that CLI's own restricted mode: Claude Code with
`--permission-mode dontAsk` and a tool allowlist, Codex with its
`workspace-write` sandbox, and Gemini CLI inside its Docker or Podman sandbox.
Those boundaries are only as strong as the provider's implementation. A role
with `unsafe_bypass: true` runs with approvals and sandboxing disabled, which
is the same as running that CLI directly with full access to the target
repository and your user account.

The dispatcher should fail closed on parse errors and ambiguous routing, but it
cannot make an unsafe provider CLI configuration safe.
