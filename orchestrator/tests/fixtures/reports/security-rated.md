---
task_id: add-export-button
phase: security
step: 6
date: 2026-10-06
verdict: findings
commit: 0123abc
---

# Security: add an export button

Summary for the owner: Security found 3 gaps. One can be abused now.
Capped verdict: no

## Summary
The export endpoint was attacked on the local test server. One authorization gap is real.

## Required categories
| Category | Result | Evidence |
| --- | --- | --- |
| Injection | pass | `web/export.py:52` binds the id as a parameter |
| Input validation | pass | `web/export.py:30` |
| Output sanitization | fail | finding 2 |
| Authentication and authorization | fail | finding 1 |
| Secrets and sensitive data | pass | nothing logged |

## Dependencies and configuration
None added.

## Attacks (verified)
| Input or output path | Target | Attack and steps | Expected | Observed |
| --- | --- | --- | --- | --- |
| report id | local test server | another team's id | 404 | 200 and the file |

## Not verified
| Item | Category | Why not | Check a human should run |
| --- | --- | --- | --- |
| Rate abuse | Authentication and authorization | no load tool in the profile | 100 exports in a minute |

## Not covered
The mobile client, which does not have the button yet.

## Rated findings

| # | Severity | Finding | Place |
| --- | --- | --- | --- |
| 1 | high | Any signed-in person can export another team's report by typing its id. | web/export.py:41 |
| 2 | Medium | A cell that starts with "=" is run as a formula by spreadsheet apps. | web/export.py:88 |
| 3 | low | The error page names the framework. | whole change |

## Findings
1. **web/export.py:41**: no team check. Add it and a test that another team's id gets 404.
2. **web/export.py:88**: prefix cells that start with `=`, `+`, `-` or `@`.
3. The error page: use the plain 500 page.

## Verdict rationale
Finding 1 can be hit now but needs a sign-in, so `findings`, not critical.
