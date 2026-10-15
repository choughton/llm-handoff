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
- Keep `auto_push` disabled unless you explicitly want the dispatcher to publish
  commits.
- Treat generated handoff instructions as untrusted until validated.
- If `.dispatch.lock` exists, confirm no other dispatcher is active before
  removing it manually.

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
