# Examples

Two things live here: a small app to try Relay on, and a brief to run against it. The README's quick start registers a copy of the app and starts that task.

## `slugify/`: the sample app

A short Python program that turns a title into a URL slug:

```console
$ python3 slugify.py "Creme Brulee, Two Ways!"
creme-brulee-two-ways
```

It has five tests and a `.relay/profile.yaml` that tells Relay how to run them. The tests use Python's own `unittest`, so nothing needs installing. Copy it somewhere, `git init` it, and register it with `relay register`.

## `briefs/add-max-length.md`: the brief

A `feature` sized brief that is not risky and has no deploy: let a caller cap a slug's length, cutting between words where it can. The header at the top decides the chain. A feature starts with the Designer, then goes to the Builder and the Reviewer; the Architect runs only if the Designer says the structure changes. Because the brief says it is not risky, Relay adds no Tester or Security, and because nothing deploys, no Ops.

## What a run leaves

Each task gets a folder under `~/Projects/.relay/tasks/<task-id>/` holding the brief and one numbered report per step, such as `01-designer.md`, `02-builder.md` and `03-reviewer.md`. A step that answers a send-back is named for its round, such as `04-builder-fix-1.md`. Each report starts with a short header giving its phase, step and verdict, and Relay routes on that verdict. Relay's own test runs, the gates and your decisions are recorded in the ledger; `relay status <task>` shows the history and `relay show <task> <report>` prints any report.
