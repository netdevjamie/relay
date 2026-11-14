# Examples

Three things live here: a small app to try Relay on, a brief to run against it, and a real run of that brief, saved exactly as Relay left it.

## `slugify/`: the sample app

A short Python program that turns a title into a URL slug:

```console
$ python3 slugify.py "Creme Brulee, Two Ways!"
creme-brulee-two-ways
```

It has five tests and a `.relay/profile.yaml` that tells Relay how to run them. The tests use Python's own `unittest`, so nothing needs installing. Copy it somewhere, `git init` it, and register it with `relay register`.

## `briefs/add-max-length.md`: the brief

A `feature` sized brief that is not risky and has no deploy: let a caller cap a slug's length, cutting between words where it can. The header at the top decides the chain. A feature goes Designer, Builder, Reviewer. Because the brief says it is not risky, Relay adds no Tester or Security, and because nothing deploys, no Ops.

## `recorded-run-add-max-length/`: a real run

This is the task folder from running that brief on the sample app, from start to finish, with nothing edited except the local paths. When it was recorded, the sample's tests ran with pytest, which wrote the counts Relay shows; the sample now uses `unittest`, so a new run records its counts as unknown. Read the files in order:

| File | What happened |
|------|---------------|
| `brief.md` | The brief above, as the owner wrote it. |
| `01-designer.md` | The Designer turns the request into a full brief, with worked examples for every edge case and one question for the owner. Relay stops for the owner here. |
| `02-builder.md` | The Builder adds the cap and 44 tests, and shows that each new test fails when its rule is broken. |
| `02-test-run.log` | Relay's own test run on the Builder's commit: 49 passed. A role saying "tests pass" is never enough. |
| `03-reviewer.md` | The Reviewer approves with one medium finding: titles that start with a hyphen stopped working on the command line. Relay stops for the owner. |
| `04-builder-fix-1.md` | The owner sent it back. The Builder fixes the medium and both lows in fix round 1. |
| `04-test-run.log` | Relay's test run on the fix: 69 passed. |
| `05-reviewer.md` | The Reviewer checks each fix and approves. The task is done. |
| `run.yaml` | The task's state as Relay saved it: phase, verdicts, retries and history. |
| `commits.txt`, `result.diff` | The four commits the Builder made and the full change. |

The owner made three decisions, all from the gate card (`relay waiting`, `relay card`, `relay decide`):

1. At the Designer's gate, a note: keep whole words, add one README example, use argparse. Then approve.
2. At the Reviewer's gate, send back: "a title that starts with a hyphen must still print a slug, as it did before. Fix both lows too."
3. Nothing at the end. The second review approved it, and Relay closed the task.

## What it shows

- **Every step is a file.** Each report starts with a short header naming its phase and verdict, and Relay routes on that verdict. Nothing is hidden in a chat log.
- **Relay checks the work itself.** After every Builder step it runs the project's tests in the task's checkout, at its commit, and records the result; it refuses a checkout with uncommitted changes. A failing run sends the work back to the Builder before any review.
- **The owner decides at the gates.** Approve, send back with a note, or abort. Notes go to every later role.
- **A fix round is normal.** The send-back made step 4 `04-builder-fix-1.md`. Relay stops for the owner after two fix rounds that still leave a high finding.
