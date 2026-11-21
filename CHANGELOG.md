# Changelog

## Unreleased

### Added

- The Arbiter, an eighth role. It is read-only and decides every pass with warnings, and any loop still finding a high-rated problem after two review loops on one plan. It rules move on, a targeted round, a re-plan or escalate to you. Code stops for you before a move on could close the task, reach Ops, or pass an open Security high or a failing test run, and a second re-plan stops for you too.
- Review loops are counted on the current plan (they start again after a re-plan), and `relay status` and the JSON API break them down by cause.
- A task's base is pinned to a commit Relay confirmed at `relay start` and checks again before every role starts. A base that no longer contains that commit stops the task.
- Relay's own test runs stop for you when the branch changed the test command or a test harness file, or when fewer tests ran than before. A run that exits 0 with no tests run counts as failed.
- Seen to fail: when a branch adds or changes tests, Relay runs them over the base's code and shows on the gate which of them failed there.
- Relay's extra test runs happen in a throwaway clone, never in your checkout.
- `test_harness` and `test_clean_checkout` in a project's profile: name extra harness files, and run Relay's tests in a fresh checkout of the commit.
- A sizing hint after each Builder round, from the paths the branch changed. The Reviewer must answer it on a task that is not risky, and the Reviewer or the Arbiter can raise the sizing mid-run.
- `relay resize`: you change a task's sizing either way, with a reason. Only you can lower one.
- `risk_paths` in a project's profile adds path patterns to the sizing hint, read from the base branch.
- Under `--auto`, every role is started with its permission mode written on the call, and stopped after a time limit (two hours unless set).
- Per-role settings under `roles:` in Relay's config: `model`, `effort`, `timeout_seconds` and `permission_mode`.
- Each role is handed a reading list: the brief, the latest report of each role that has run and the report the round answers, with the rest of the task folder as optional history.
- The ledger records its own version and backs itself up before an upgrade.
- A command reference (`docs/commands.md`) with an example of every command, and a quick start you can run from a fresh clone.

### Changed

- The JSON read API is at `schema_version` 4: new gate kinds for the Arbiter and the time limit, and new status fields (see `docs/json-api.md`).
- A pass with warnings no longer stops for you on its own; it goes to the Arbiter, which escalates when the call is yours. This replaces the 0.1.0 stop after two fix rounds.

### Removed

- The legacy per-project `.workflow/` mode.
- The recorded run in `examples/`. It showed the 0.1.0 warning gate, which now goes to the Arbiter.

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
