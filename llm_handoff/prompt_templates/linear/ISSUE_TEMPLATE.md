## Linear Issue Template

The issue description is the Work Packet source. When the planner leases an
issue, it copies these fields from the description into the `HANDOFF.md`
`## Work Packet`, so write them with the same labels the dispatcher parses.
List paths one per nested item. Write each verification command as its own
nested item with no prose around it, and put notes in the Context.

Example issue description:

```markdown
## Work Packet

- **Objective:** Add a `--limit` option to the `report` command that caps the number of rows printed.
- **Files in scope:**
  - `src/report.py`
  - `tests/test_report.py`
- **Files out of bounds:**
  - `src/storage.py`
- **Context:** The `report` command prints every row today. Keep the default output unchanged when `--limit` is not given.
- **Verification command:**
  - python -m pytest tests -q
- **Expected next route:** auditor
```
