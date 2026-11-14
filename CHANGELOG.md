# Changelog

## 0.1.0

First public release.

- A chain of seven roles (Designer, Architect, Builder, Reviewer, Tester, Security, Ops), sized by the brief: `fix`, `feature` or `project`, with Tester and Security added for risky work and Ops only when the brief deploys.
- Relay runs the project's tests itself after each Builder step and each Tester pass, and records the result in a SQLite ledger.
- Gates you can decide from a phone: `relay waiting`, `relay card`, `relay show` and `relay decide`.
- Fix rounds stop for the owner after two rounds that still find a high-rated problem.
- Prototype mode: `relay proto` and `relay promote`.
- A JSON read API (`relay list --json`, `relay status --json`).
- `relay doctor` checks an install end to end.
- `setup.sh` warns when another `relay` on your PATH would run instead, and says which one runs.
- A security policy (`SECURITY.md`): report vulnerabilities privately through GitHub.
- A sample app, a sample brief and a recorded real run in `examples/`.
