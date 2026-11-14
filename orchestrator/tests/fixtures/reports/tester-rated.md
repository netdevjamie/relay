---
task_id: add-export-button
phase: tester
step: 5
date: 2026-10-06
verdict: pass_with_concerns
commit: 0123abc
---

# Test: add an export button

Summary for the owner: Everything that can be run here passes. One criterion needs a person with a real spreadsheet app.

## Verified (ran it, saw the result)
### Suite
Commit 0123abc. `make test` twice: 212 passed both times. Matches Relay's recorded run.
### Acceptance criteria
| Criterion | How verified | Result |
| --- | --- | --- |
| 1. The button downloads a file | `make test -k export` | pass |
| 2. The file opens in a spreadsheet app | not here | not verified |
### Edge cases
| Case | Steps | Expected | Actual |
| --- | --- | --- | --- |
| Empty report | export with no rows | header row only | header row only |

## Missing pieces
Checked, none.

## Not verified (reasoned about, or needs a manual check)
| Item | Acceptance criterion (if any) | Why not here | Manual check needed |
| --- | --- | --- | --- |
| Opens in a spreadsheet app | 2 | No such app on this machine | Open the file and read cell A1 |

## Tests added
None.

## Manual test plan
`docs/manual-tests/add-export-button.md`, covers criterion 2, not yet run.

## Rated findings

| # | Severity | Finding | Place |
| --- | --- | --- | --- |
| 1 | medium | Nobody has opened an exported file in a real spreadsheet app, so criterion 2 is unproven. | docs/manual-tests/add-export-button.md, step 4 |
| 2 | low | A report name with a comma is quoted in a way one old app shows as two cells. | web/export.py:90 |

## Notes
- The fixture names could be shorter.

## Verdict rationale
The only concern that stops the task is the unrun manual plan. That stop is expected.
