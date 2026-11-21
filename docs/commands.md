# Commands

Every `relay` command, with what it is for and an example. `relay --help` prints the same list in short. Commands that take a task id read the task folder `~/Projects/.relay/tasks/<task-id>/` and, with `persistence_backend: sqlite`, the ledger beside it.

## Running a task

### `relay start`

Starts a task from its brief, in the checkout you run it from (or the one `--worktree` names). That checkout must be on a branch of its own; Relay makes no worktree. Prints the first role's prompt.

```bash
git switch -c add-max-length
relay start add-max-length --repo slugify
relay start add-max-length --repo slugify --worktree ../slugify-add-max-length --base main
relay start add-max-length --repo slugify --auto      # start the first role itself
```

`--deploy` is a first guess that the task deploys; the Architect's plan decides whether Ops runs.

### `relay next`

Reads the latest report's verdict, runs the project's tests when a Builder or Tester has finished, and either prints the next role's prompt, starts it (`--auto`), or stops for you with a gate card.

```bash
relay next add-max-length
relay next add-max-length --auto
```

### `relay status`

The task's history, sizing, the owner's notes, Relay's own test runs and the Arbiter's rulings.

```bash
relay status add-max-length
relay status add-max-length --json     # docs/json-api.md
```

### `relay list`

Every task across every registered repo, with its phase and status.

```bash
relay list
relay list --json
```

### `relay gate-check`

Says whether a task is waiting, and on whom: an agent, you, or nobody.

```bash
relay gate-check add-max-length
relay gate-check add-max-length --json
```

### `relay validate`

Checks a task's brief, run state and reports for errors: missing frontmatter, a verdict the phase does not have, a gap in the step numbers.

```bash
relay validate add-max-length
```

## Deciding at a gate

### `relay override`

Moves the task to a phase you name. At a gate this is your approval; `done` closes the task.

```bash
relay override add-max-length builder
relay override add-max-length done --note "Accepted; the low is for a later task."
```

### `relay send-back`

Sends the latest finished step back for another round. It is not an approval, and the count of automatic rounds for that phase starts again.

```bash
relay send-back add-max-length --note "Keep whole words when you cut."
relay send-back add-max-length --note-file note.txt
```

### `relay note`

Records a decision or a fact that every later role is given. Moves nothing.

```bash
relay note add-max-length --note "Use argparse for the command line."
```

### `relay abort`

Abandons the task. Nothing is deleted.

```bash
relay abort add-max-length
```

### `relay resize`

Changes the task's sizing either way. Only you lower one; a Reviewer or the Arbiter can only raise it. Needs a reason, and the SQLite ledger.

```bash
relay resize add-max-length --risky yes --reason "It now parses user input."
relay resize add-max-length --size fix --risky no --reason "Docs only after all."
```

## From your phone

These four print text that fits a phone screen, for the Claude Code session that drives Relay to show you through Remote Control (README, "From your phone").

### `relay waiting`

Every task waiting on you, with why it stopped and its step.

```bash
relay waiting
```

### `relay card`

One task's gate: the report's summary line, its findings, what you are asked, and each choice by name.

```bash
relay card add-max-length
```

### `relay show`

One report, or the brief, in full, by name.

```bash
relay show add-max-length brief
relay show add-max-length 03-reviewer
```

### `relay decide`

Makes a choice from the card, but only if the task still waits at that step and that kind of gate. It runs the same command you would type (`override`, `send-back`, `abort` or `note`) and prints what was recorded.

```bash
relay decide add-max-length send-back --step 3 --gate approval --note-file note.txt
relay decide add-max-length move-to --step 3 --gate approval --to tester
```

## Setting up

### `relay register`

Adds a repo to Relay's registry under an alias, and writes a `.relay.yaml` naming that alias into the repo if it has none. Commit that file.

```bash
relay register ~/slugify --alias slugify
```

### `relay profile`

Checks a repo's `.relay/profile.yaml`, which tells Relay how to run its tests (`docs/profile.md`). Exits 0 only when the profile exists and has no errors.

```bash
relay profile slugify
relay profile ~/slugify --json
```

### `relay doctor`

Checks that the install can run a task: Python, git, the `claude` command, that the role prompts in `~/.claude/agents` match the install, the write-guard hook, the registry and the ledger. Each check that is not ok says how to fix it. Exits 1 if any check fails.

```bash
relay doctor
```

### `relay recover`

Creates and repairs the SQLite ledger. `--init` creates an empty ledger when there is none; `./setup.sh` already runs it once when the install has no ledger. `--from-projections` compares task folders (from the files backend, or from before the ledger) with the ledger and, with `--apply`, imports them; `--decisions FILE` gives explicit choices for tasks that do not import cleanly. `--regenerate-projections` rewrites each task folder's `run.yaml` from the ledger.

```bash
relay recover --init
relay recover --from-projections
relay recover --from-projections --decisions decisions.yaml --apply
relay recover --regenerate-projections add-max-length
```

## Prototypes

### `relay proto`

A sketch to answer "is this worth building?", with no chain: `start` records a time box and prints the `proto/<name>` branch to build on, `done` marks it ready to show (it needs a screenshot in the evidence folder), and `list` shows every prototype and where it went.

```bash
relay proto start checkin --repo my-app --idea "What it is, who it is for, what wow looks like." --hours 4
relay proto done checkin
relay proto list
```

### `relay promote`

Turns a shown prototype into a real task: writes its brief, sized and with its risk set, and attaches the prototype's screenshots as its spec. The prototype's code is never merged; `relay start` refuses a `proto/` branch.

```bash
relay promote checkin --size feature --risky no
relay promote checkin --size project --risky yes --deploy --as checkin-v1
```
