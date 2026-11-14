# Project profiles

Each repo that Relay works on commits `.relay/profile.yaml`. It tells Relay's roles how this repo is tested, built and deployed, so that knowledge lives in the repo and not in a prompt or someone's memory.

```
relay profile <repo-alias-or-path>          # check it
relay profile <repo-alias-or-path> --json   # the same, as JSON (docs/json-api.md)
```

The exit code is 0 only when the profile exists and has no errors. Start from `templates/profile.yaml`. Relay's own profile is `.relay/profile.yaml` in this repo.

## Status

Running: reading and checking a profile, the `relay profile` command, and test evidence (below). The role prompts already tell each agent to read the profile.

Not built: Relay using the deploy targets to recognise or run a deploy.

One rule is fixed already because it shapes what a profile may promise: **an approval never trusts the working copy's profile.** If Relay comes to run approved deploys, it will read the profile from the approved commit and record the target's exact command with the approval, so editing `command` after approval cannot change what runs.

## Rules

- **Strict.** An unknown key is an error, so a typo can't be silently ignored.
- **Read-only.** Relay never runs a command from the profile while checking it. The only profile command Relay runs today is `commands.test`, after the Builder and the Tester (see Test evidence).
- **No secret values.** A secret entry holds a name and a location, nothing else.
- **How commands will be run.** Every command in a profile (`commands.*`, a target's `command` and `smoke`, a CLI's `check`) is one line of shell, run with `sh -c` from the repo root. `cd sub && make test` is fine. The test command is stopped after `test_timeout_seconds` (Relay's own config, default 1800) and the run counts as failed. The meaning of the existing strings will not change within schema version 1.
- **Spelling counts.** When `platforms` is listed, a deploy target's or store's `platform` must be one of them, exactly as spelt.

## Test evidence

When the Builder reports `ready-for-review`, or the Tester reports `pass` or `pass_with_concerns`, `relay next` runs `commands.test` itself in the task's checkout before it routes anything, and records the run in the ledger as a `test_run` event: commit, command, outcome, exit code, duration and counts. The output is saved in the task folder as `NN-test-run.log`.

| Outcome | Meaning | What `relay next` does |
| --- | --- | --- |
| `passed` | The command exited 0. | Routes on the agent's verdict. |
| `failed` | The command exited non-zero. | Back to the Builder, with the end of the output. |
| `timed_out` | The command ran past the time limit and was stopped. | Back to the Builder. |
| `uncommitted` | The run does not describe a commit: the checkout had uncommitted changes (nothing was run), or the test command left unignored files behind or moved the branch. | Back to the Builder. |
| `profile_broken` | The branch removed or broke a profile that its base has, so there was nothing valid to run. | Back to the Builder. |
| `not_run` | The repo has no valid profile (and neither does the task's base), is not a git checkout, or the task has no working directory. | Routes on the agent's verdict; the next agent is told there is no recorded run. |

- Going back to the Builder this way counts toward the retry limit, like any other loop back.
- Pass or fail is the exit code. Counts come from `test_report` when the run wrote that file; otherwise they are recorded as unknown. A report left over from an earlier run is ignored.
- The profile is read from the task's checkout, since a task may change how its repo is tested. That lets the branch under test decide what "the tests" are, so a passing run whose test command or `test_report` is not what the branch started from (or whose base has no profile) **stops for the owner** at an approval gate that shows both commands. Approving at that gate (`relay override <task> <next phase>`) records a `test_command_accepted` event for that exact command and report path, which covers the rest of the task. No other approval counts, an override to a different phase accepts nothing, and a new command asks again. The comparison is with the commit where the branch left its base, so a later profile change on the base is not held against the task.
- `relay profile` warns when `test_report` is not ignored by git, because the report file would then make every run `uncommitted`.
- A failed run that also leaves files behind stays `failed`; the leftover files are added to its reason.
- A run already recorded for the same step and commit is reused.
- The files backend keeps no events, so it gets no run and no record. Test evidence needs `persistence_backend: sqlite` in Relay's `config.yaml`.

Known limit: only the command text and report path are compared. A branch that keeps `npm test` but edits what `npm test` does (a script, a Makefile, a test config that skips things) is not caught here; that is what the recorded counts and the Reviewer are for.

Not built yet: refusing to complete or merge a task without a passing recorded run at the final commit; flagging a drop in test count or new skips automatically (the counts are recorded and shown; a person or the Reviewer compares them).

## `test_files` and `known_flaws_doc`

Older versions of Relay read these two settings. Both are still accepted, so a profile that has them stays valid, and nothing reads them.

## Limits on reading a profile

`relay profile` and Relay's own test run read the profile in the checkout, and the test run the profile at the task's base. A profile larger than 64 KB, or one that holds a YAML anchor, alias or merge key (`&name`, `*name`, an unquoted `<<`) or a tag (`!!str`, `!`), more than 5,000 values, lists and mappings, or lists and mappings nested more than 10 deep, or that cannot be read as one YAML document, or that holds a value YAML cannot build, such as the date `2026-99-99`, is one they report they could not read. The test run takes no test command from such a profile at the task's base, so the branch's is flagged as not the base's, and where `relay next` asks the owner to confirm the branch's command it says why, in a line that starts "The base branch's profile could not be read:".

## Threat model

Beside the profile, each repo may commit `.relay/threat-model.md`: what is trusted, and what the attack surface is, for that repo. It may add notes, such as which data is sensitive. It holds no rating rules: those are in the Builder, Reviewer, Tester and Security role files, the same words in each, under "Threat model and ratings".

- **Read by its fixed name.** Every role reads `.relay/threat-model.md` beside the profile. Relay's code does not read it.
- **The base branch's copy governs.** The roles read the copy on the task's base branch with `git show {base}:.relay/threat-model.md`, because the base is trusted and the task branch is not. A branch that adds or changes the file does not change the model for its own task; its text governs from the next task after merge. Security names any change a branch makes to the file as a finding.
- **When there is none.** The roles work to the strictest default, which the four rating role files define: only the owner and Relay's installed files are trusted, and everything else is attack surface. Security writes a finding that says the file is missing, rated low and marked `Record`.
- **Whose it is.** A repo's threat model is the owner's trust decision. No role writes one for a repo, and no fix round is sent to add one.

Relay's own is `.relay/threat-model.md` in this repo.

## `.relay.yaml`: deploy notes for Ops

A repo may also commit `.relay.yaml` in its root (the template is `templates/relay.yaml`). It holds one setting, `ops_addendum`: the name of a file in the repo root whose text Relay adds to the Ops role's prompt, for deploy context the profile has no field for. Relay never writes the file, and no other phase reads it.

## Fields

| Key | Required | What it holds |
| --- | --- | --- |
| `schema_version` | yes | `1` |
| `platforms` | no | List of what the repo ships, such as `web`, `api`, `ios`, `android`, `cli`. |
| `commands` | yes | `test` (required), and optionally `build`, `lint`, `audit` (dependency check), `start` (run the app locally). |
| `test_report` | no | Path, inside the repo, of the JUnit XML file the test command writes. Relay reads test counts from it. Keep the file out of git (`.gitignore`). |
| `flaky_check_runs` | no | How many times the Tester reruns the suite. A whole number, 1 or more. The Tester's default is 2. |
| `test_files` | no | Accepted and no longer read. See `test_files` and `known_flaws_doc`. |
| `known_flaws_doc` | no | Accepted and no longer read. See `test_files` and `known_flaws_doc`. |
| `required_clis` | no | List of `{name, check}`. `check` is a command that exits 0 only when the CLI is installed and logged in. |
| `deploy_targets` | no | List of targets, below. |
| `stores` | no | List of `{platform, app_id, track, release_script}`. `platform` and `app_id` are required. |
| `secrets` | no | List of `{name, location}`. `location` starts with `keychain:`, `keepass:` or `env:`. After `env:` comes a variable name only. |
| `security_test_targets` | no | List of `{name, url}`: the only environments Security may attack. `url` is a full `http://` or `https://` address. None listed means no live attacks. |
| `observability` | no | Mapping of a name (`logs`, `errors`, `analytics`) to where to look. |

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
