# Project profiles

Each repo that Relay works on commits `.relay/profile.yaml`. It tells Relay's roles how this repo is tested, built and deployed, so that knowledge lives in the repo and not in a prompt or someone's memory.

```
relay profile <repo-alias-or-path>          # check it
relay profile <repo-alias-or-path> --json   # the same, as JSON (docs/json-api.md)
```

The exit code is 0 only when the profile exists and has no errors. Start from `templates/profile.yaml`. Relay's own profile is `.relay/profile.yaml` in this repo.

## Status

Running: reading and checking a profile, the `relay profile` command, and test evidence (below). The role prompts already tell each agent to read the profile.

Not built: `relay doctor` checking a profile's required CLIs, and Relay using the deploy targets to recognise or run a deploy.

One rule is fixed already because it shapes what a profile may promise: **an approval never trusts the working copy's profile.** If Relay comes to run approved deploys, it will read the profile from the approved commit and record the target's exact command with the approval, so editing `command` after approval cannot change what runs.

## Rules

- **Strict.** An unknown key is an error, so a typo can't be silently ignored.
- **Read-only.** Relay never runs a command from the profile while checking it. The only profile command Relay runs today is `commands.test`, after the Builder and the Tester (see Test evidence).
- **No secret values.** A secret entry holds a name and a location, nothing else.
- **How commands will be run.** Every command in a profile (`commands.*`, a target's `command` and `smoke`, a CLI's `check`) is one line of shell, run with `sh -c` from the repo root. `cd sub && make test` is fine. The test command is stopped after `test_timeout_seconds` (Relay's own config, default 1800) and the run counts as failed. The meaning of the existing strings will not change within schema version 1.
- **Not the profile's to set.** A profile cannot set a role's permission mode, model, effort or time limit. They are Relay's own config (`config.yaml` in the install, documented in the README under "Running roles automatically"), so a branch cannot change how its own roles run.
- **Spelling counts.** When `platforms` is listed, a deploy target's or store's `platform` must be one of them, exactly as spelt.

## Test evidence

When the Builder reports `ready-for-review`, or the Tester reports `pass` or `pass_with_concerns`, `relay next` runs `commands.test` itself in the task's worktree before it routes anything, and records the run in the ledger as a `test_run` event: commit, command, outcome, exit code, duration and counts. The output is saved in the task folder as `NN-test-run.log`.

| Outcome | Meaning | What `relay next` does |
| --- | --- | --- |
| `passed` | The command exited 0. | Routes on the agent's verdict. |
| `failed` | The command exited non-zero, or exited 0 with no tests run or with no readable report where the profile names one (`exit_code` is then 0 and the reason says which). | Back to the Builder, with the end of the output. |
| `timed_out` | The command ran past the time limit and was stopped. | Back to the Builder. |
| `uncommitted` | The run does not describe a commit: the worktree had uncommitted changes (nothing was run), or the test command left unignored files behind or moved the branch. | Back to the Builder. |
| `profile_broken` | The branch removed or broke a profile that its base has, so there was nothing valid to run. | Back to the Builder. |
| `not_run` | The repo has no valid profile (and neither does the task's base), is not a git checkout, or the task has no working directory. | Routes on the agent's verdict; the next agent is told there is no recorded run. |

- Going back to the Builder this way counts toward the retry limit, like any other loop back.
- **Counts.** Counts come from `test_report` when the run wrote that file. A report left over from an earlier run is ignored. "Tests run" is the report's tests minus its skipped, so a test turned into a skip counts as one fewer.
  - **0 tests.** A run that exits 0 and shows 0 tests run is `failed`, with a reason that begins "0 tests ran".
  - **A named report that is missing.** A run that exits 0 when the profile names a `test_report`, but did not write it or wrote one Relay cannot read, is `failed`, with a reason that begins "The profile names {path}, but this run did not write a readable report".
  - **No report named.** The run routes on its exit code as before, and its summary says "counts unknown (the profile names no test report), so Relay could not check for 0 or fewer tests".
  - **Fewer tests.** A passing run with fewer tests run than at the base, or than the latest earlier run of the task that had counts, **stops for the owner**. The reason names the larger drop: "N fewer tests ran than at the base (B before, H now)" or "... than the previous run". The base is counted by running the base's own command and report at the commit where the branch left its base, in a scratch checkout (below), at most once per task for that commit; its output is saved as `NN-test-run-base.log`. When the base's count cannot be had (the base names no report, its run timed out or wrote no readable report), only the comparison with the previous run is made, and the summary says "The base's test count is unknown (why)".
- **The command and the harness.** The profile is read from the task's worktree, since a task may change how its repo is tested. That lets the branch under test decide what "the tests" are, so a passing run **stops for the owner** at an approval gate when its test command or `test_report` is not what the branch started from (or its base has no profile), or when the branch changed a **harness file** against the commit where it left its base, or when Relay could not tell which harness files it changed (the commit where the branch left its base cannot be found, as for a branch with no history shared with it, or git could not list its changes, or the profile at the base commit cannot be read; the reason and summary say "Harness files not checked: ..."). Harness files are decided from that commit, and the `test_harness` list from the profile at the base commit Relay confirmed for the task (see `relay start`'s Branch line), never from the branch:
  - a file the base's test command names, from the repo root or from a folder the command `cd`s into (a folder never counts);
  - `package.json` when the command runs `npm`, `npx`, `yarn` or `pnpm`, and the `Makefile` when it runs `make`;
  - common test runner configuration, by file name: `conftest.py`, `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `noxfile.py`, `jest.config.*`, `vitest.config.*`, `vitest.workspace.*`, `karma.conf.*`, `.mocharc*`, `playwright.config.*`, `cypress.config.*`, `phpunit.xml`, `phpunit.xml.dist`, `*.runsettings`, `Directory.Build.props`, `Directory.Build.targets`;
  - anything the `test_harness` list in the profile at the base commit names;
  - a submodule: any change to `.gitmodules`, and any submodule the branch adds, removes or moves to another commit, since the run loads files from inside it that the branch's diff lists only as one path;
  - the module a command runs with `python -m X`, as `X.py` or anything under `X/` in a folder the command runs from, since Python imports it there in place of the installed one. A module that one imports in turn (pytest's `_pytest`, say) is not covered.
  
  Names and paths are compared in any case (`PYTEST.INI` counts as `pytest.ini`), since the Mac's disk ignores case and pytest reads such a file. CI configuration is not harness: Relay's run does not execute it. A harness file the branch renamed or deleted still counts, by its old path.
- **The owner's acceptance.** The gate shows the changed command (both commands), the changed harness files (up to five, then "and N more") and the drop. Approving at that gate (`relay override <task> <next phase>`) records one `test_command_accepted` event for exactly what it showed: the command and report path, and, when the run flagged them, a digest of the changed harness files as they are at that commit (`harness_digest`), the number of tests run (`tests_run`), and the commit whose harness Relay could not check (`harness_unchecked_at`). An acceptance of a changed command alone holds only the command, the report path and the commit. No other approval counts, and an override to a different phase accepts nothing. At an Arbiter escalation the gate gives the same reason when a choice moves past the report the Arbiter ruled on, and `relay next` and `relay gate-check` print the same lines; taking such a choice (to a later phase, or done) records the acceptance, and nothing else at an escalation does. A new command asks again; a later commit that changes a harness file again, or a different one, asks again; a later run with fewer tests than the accepted number asks again; when the harness could not be checked, a later commit asks again. A later commit that changes nothing in the harness, or a run with the same number of tests or more, does not. The test command is compared with the one at the commit where the branch left its base, so a later change to the base's command is not held against the task. The `test_harness` list and `test_clean_checkout` are read from the base commit Relay confirmed for the task instead. Relay records that commit at `relay start` and confirms it again before every step (for a remote-tracking base, by asking the remote itself), moving it only forward, so one the owner adds to the base later applies to a running task once fetched, and a branch built on an older commit of the base cannot escape it. A case twin of the base, or a tag named like it, never moves it.
- **Clean checkout.** With `test_clean_checkout: true` in the profile at the base commit or in the branch's (a branch cannot turn the base's off; when the profile at the base commit cannot be read, the profile at the commit where the branch left its base decides, and the run waits for the owner as above), the test command runs in a fresh checkout of the run's commit, with no ignored or untracked files, under the system temp folder. The report is read there, and the checkout is deleted after the run whatever its outcome. The task's worktree should not be touched by the run; one that is changed or moved to another commit afterwards makes the run `uncommitted`, as without clean mode. The test command must install what it needs itself. With it off, the run notes the top-level ignored paths present in the worktree ("Ran with ignored files present: ..."); the note never changes the outcome.
- **Seen to fail.** When a passing run's branch added or changed test files against its base (found by path: a `test`, `tests`, `__tests__`, `spec` or `specs` folder, or a name such as `test_*.py`, `*_test.go`, `*.test.*`, `*.spec.*`, `*Test.java`), Relay runs the base's command over the base's code with the branch's versions of those files, in a scratch checkout, and records each of their test cases as failed, passed, did not run, or could not check at the base. Cases are matched to files by the JUnit report's `file` attribute or the leading words of its `classname`. A case that errors at the base (an import that is not there yet, say) is "could not check", never "failed". When no test at the base passed, failed or was skipped (pytest runs none once one file fails to load), every case it does not report is "could not check" too, with the reason "the run at the base stopped before any test ran". A changed test file whose name is not UTF-8 makes the whole check "could not check". With no readable report it records "could not check: no test report". The result is in the summary and on the gate ("Seen to fail at base: 3 failed, 1 passed, 0 could not check", then the tests that passed at base), its output is saved as `NN-test-run-seen-to-fail.log`, and it **never changes routing**.
- **Scratch checkouts.** The base count, Seen to fail and clean mode each use a `git clone --shared` of the task's repository in a new `relay-scratch-*` folder under the system temp folder, with git hooks switched off. They never run in the task's worktree or the owner's main checkout, never stash, and are deleted afterwards, also on failure and on timeout. Each uses the same time limit as the main run, so one step can take up to about three times `test_timeout_seconds`.
- **Timeout.** On the time limit the run's whole process group is sent SIGTERM, given 10 seconds to exit, and then sent SIGKILL if anything is left. The outcome is `timed_out`.
- `relay profile` warns when `test_report` is not ignored by git, because the report file would then make every run `uncommitted`.
- A failed run that also leaves files behind stays `failed`; the leftover files are added to its reason.
- A run already recorded for the same step and commit is reused.
- The legacy files backend keeps no events, so it gets no run and no record.

Known limits: harness files and test files are found by the rules above, not by reading what the command really runs, so a harness file none of them names (a script that the named script calls, say) is not caught unless the base profile lists it in `test_harness`. Any branch code the run executes can also write its own report and exit 0. Counts and Seen to fail need a JUnit report. Scratch checkouts lack ignored files such as installed dependencies, so the base may count fewer tests than really exist (missing a drop, never causing a false stop) and Seen to fail may record "could not check". The full list is in `docs/known-limits.md`.

Not built yet: refusing to complete or merge a task without a passing recorded run at the final commit.

## `test_files` and `known_flaws_doc`

Both are accepted, so an older profile that has them stays valid, and nothing reads them.

**Limits.** `relay profile` and Relay's own test run read the profile in the worktree, and the test run the profile at the task's base. A profile larger than 64 KB, or one that holds a YAML anchor, alias or merge key (`&name`, `*name`, an unquoted `<<`) or a tag (`!!str`, `!`), more than 5,000 values, lists and mappings, or lists and mappings nested more than 10 deep, or that cannot be read as one YAML document, or that holds a value YAML cannot build, such as the date `2026-99-99`, is one they report they could not read. The test run takes no test command from such a profile at the task's base, so the branch's is flagged as not the base's, and where `relay next` asks the owner to confirm the branch's command it says why, in a line that starts "The base branch's profile could not be read:".

## Threat model

Beside the profile, each repo may commit `.relay/threat-model.md`: what is trusted, and what the attack surface is, for that repo. It may add notes, such as which data is sensitive. It holds no rating rules: those are in the Builder, Reviewer, Tester and Security role files, the same words in each, under "Threat model and ratings".

- **Read by its fixed name.** Every role reads `.relay/threat-model.md` beside the profile. Relay's code does not read it.
- **The base commit's copy governs.** The roles read the copy at the task's base commit, the commit Relay confirmed for the base and names in the role's prompt, with `git show <commit>:.relay/threat-model.md`, because the base is trusted and the task branch is not. A branch that adds or changes the file does not change the model for its own task; its text governs from the next task after merge. Security names any change a branch makes to the file as a finding.
- **When there is none.** The roles work to the strictest default, which the four rating role files define: only the owner and Relay's installed files are trusted, and everything else is attack surface. Security writes a finding that says the file is missing, rated low and marked `Record`.
- **Whose it is.** A repo's threat model is the owner's trust decision. No role writes one for a repo, and no fix round is sent to add one.

Relay's own is `.relay/threat-model.md` in this repo.

## `risk_paths`

After each Builder round, Relay works out a sizing hint: which risk categories the paths the task's branch changed since it left its base look like they touch. The Reviewer gets it with the task's sizing and judges the diff; the hint never changes routing by itself (see `orchestrator/risk_hint.py` and the README's sizing line).

- **The categories.** `auth`, `input` (input handling), `payments` (money or payments), `schema` (migrations or schema), `production_data`, `secrets` and `deploy` (deploy config).
- **The defaults.** General naming conventions only, such as `*auth*`, `*parse*`, `*migrat*`, `.env` or `.github/workflows/*`, kept in `orchestrator/risk_hint.py` as `DEFAULT_PATTERNS`. They name no project.
- **Matching.** In any case. A pattern without `/` matches a folder name or the file name anywhere in a path; one with `/` matches the whole path, where `*` also crosses folders. Names only: no file's content is read.
- **Additions only.** `risk_paths` maps a category to a list of 1 to 50 patterns, each text of at most 200 characters on one line. They are added to the defaults; nothing removes a default. An unknown category is an error.
- **Read from the base.** Relay reads `risk_paths` from the profile at the base commit Relay confirmed for the task, never from the branch. That commit only moves forward, so a branch cannot narrow or widen its own hint, and a pattern the owner adds to the base later applies to the next hint worked out after Relay has confirmed the newer commit; a hint already recorded for a Builder step at the same commit is kept as it was. A base profile Relay cannot read, or a `risk_paths` that fails the check, adds nothing; the hint uses the defaults and says why.

```yaml
risk_paths:
  auth:
    - "gatekeeper_*"
  deploy:
    - "ops/release/*"
```

## Fields

| Key | Required | What it holds |
| --- | --- | --- |
| `schema_version` | yes | `1` |
| `platforms` | no | List of what the repo ships, such as `web`, `api`, `ios`, `android`, `cli`. |
| `commands` | yes | `test` (required), and optionally `build`, `lint`, `audit` (dependency check), `start` (run the app locally). |
| `test_report` | no | Path, inside the repo, of the JUnit XML file the test command writes. Relay reads test counts from it. Keep the file out of git (`.gitignore`). |
| `test_harness` | no | List of extra file names or path patterns, inside the repo, that make up how the tests run. Read from the base commit Relay confirmed for the task, so a branch built on an older commit of the base is held to it too: a branch that changes one stops for the owner. An entry without `/` matches a file name anywhere; one with `/` matches the whole path, where `*` also crosses folders. At most 200 entries. See Test evidence. |
| `test_clean_checkout` | no | `true` or `false` (default). `true` runs Relay's test run in a fresh checkout of the commit, with no ignored files. On when the profile at the base commit or the branch's profile says `true`. See Test evidence. |
| `flaky_check_runs` | no | How many times the Tester reruns the suite. A whole number, 1 or more. The Tester's default is 2. |
| `test_files` | no | Accepted and no longer read. See `test_files` and `known_flaws_doc`. |
| `known_flaws_doc` | no | Accepted and no longer read. See `test_files` and `known_flaws_doc`. |
| `required_clis` | no | List of `{name, check}`. `check` is a command that exits 0 only when the CLI is installed and logged in. |
| `deploy_targets` | no | List of targets, below. |
| `stores` | no | List of `{platform, app_id, track, release_script}`. `platform` and `app_id` are required. |
| `secrets` | no | List of `{name, location}`. `location` starts with `keychain:`, `keepass:` or `env:`. After `env:` comes a variable name only. |
| `security_test_targets` | no | List of `{name, url}`: the only environments Security may attack. `url` is a full `http://` or `https://` address. None listed means no live attacks. |
| `observability` | no | Mapping of a name (`logs`, `errors`, `analytics`) to where to look. |
| `risk_paths` | no | Mapping of a sizing-hint risk category to extra path patterns, added to Relay's defaults. Read from the base commit Relay confirmed for the task. See `risk_paths`. |

### Deploy targets

| Key | Required | What it holds |
| --- | --- | --- |
| `name` | yes | Unique within the profile. |
| `command` | yes | The deploy command. |
| `platform` | no | Which platform this target ships. |
| `requires_cli` | no | List of names, each of which must appear in `required_clis`. |
| `smoke` | no | How to prove the deploy works. Missing is a warning: Ops reports not-ready without one. |
| `rollback` | no | How to undo the deploy. Missing is a warning, for the same reason. |
| `rollback_cannot_undo` | no | What the rollback does not restore (data migrations, a store release). |

Every deploy needs the owner's approval. There is no setting to turn that off.
