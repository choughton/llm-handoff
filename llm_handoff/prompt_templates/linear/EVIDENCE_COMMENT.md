## Linear Evidence Comment

An evidence comment is a Linear comment on the leased issue whose body is one
fenced YAML block. The root key is `llm_handoff_event`, and its value is the
event name.

Fields:

- `llm_handoff_event`: the event name, from the table below.
- `version`: always `1`.
- `issue`: the issue identifier, equal to `tracker_issue`.
- `tracker_url`: the issue URL, equal to `tracker_url`.
- `producer`: the role posting the comment.
- `next_agent`: the role the handoff routes to next.
- `status`: the handoff status, from the shared status enum.
- `state_from`: optional. The issue state before this event changed it.
- `state_to`: optional. The issue state after this event changed it.
- `commit_sha`: the concrete commit SHA the evidence covers. See the
  dispatcher commit mode rule below for the one exception.
- `commands_run`: a list of the commands run in this turn.
- `output_summary`: a short summary of what those commands reported.
- `unresolved_concerns`: a list of open concerns, or an empty list.

Events and the roles that may post them:

| Event | Posted by |
|---|---|
| `lease_opened` | `planner` |
| `implementation_handoff` | `backend`, `frontend` |
| `audit_passed` | `auditor` |
| `audit_failed` | `auditor` |
| `user_attention_requested` | `planner`, `auditor` |
| `user_attention_cleared` | `planner`, `auditor` |

The events `implementation_handoff`, `audit_passed`, and `audit_failed`
require `commit_sha`, `commands_run`, `output_summary`, and
`unresolved_concerns`. Evidence must come from the current turn.

When `commit_mode` is `dispatcher`, an implementer's changes are still
uncommitted when it posts `implementation_handoff`; the dispatcher creates the
commit after the role returns. In that mode only, the `implementation_handoff`
comment sets `commit_sha: pending-dispatcher-commit`. Never write a guessed or
earlier SHA. The auditor reconciles it: the `audit_passed` or `audit_failed`
comment always carries the concrete commit SHA it reviewed, which is the
`scope_sha` the dispatcher wrote into `HANDOFF.md`.

Post the comment before you rewrite `HANDOFF.md`. Never put credentials,
API keys, or tokens in a comment.

Example:

```yaml
llm_handoff_event: implementation_handoff
version: 1
issue: ENG-123
tracker_url: https://linear.app/example/issue/ENG-123
producer: backend
next_agent: auditor
status: ready_for_review
commit_sha: 0123456789abcdef0123456789abcdef01234567
commands_run:
  - python -m pytest tests -q
output_summary: "All tests passed."
unresolved_concerns: []
```
