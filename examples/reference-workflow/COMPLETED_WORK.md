# Completed Work

Closed epics for a repository using `llm-handoff`. The finalizer moves each
closed epic here from `PROJECT_STATE.md` and appends its ledger line, so the
project state file every role reads holds only current work. Roles read this
file only when they need history.

## Completed Scope Ledger

Append one compact line per approved epic close:

```text
- **<Epic Name>** - <one-line summary>. SHA `<sha>`. Verification: <checks>.
```

## Closed Epics

The full sections of closed epics (intro, `Fit:` line, and story list), most
recent first.
