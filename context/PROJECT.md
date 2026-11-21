# Relay: project overview

Every role and every outside reviewer reads this page first. It stays one
page; details live in the docs it points to.

## What Relay is

Relay runs a piece of software work through a short chain of Claude Code
roles (Designer, Architect, Builder, Reviewer, Tester, Security, Ops,
and an Arbiter for warnings and long loops),
records every step in a SQLite ledger, and stops for the owner at gates that
code enforces, not prompts. The roles do the work, Relay's code keeps the
rules, and the owner decides what matters.

## Principles, in order

1. **Readable code above all.** Descriptive names, small single-purpose
   functions. Required on a `right` pass; a first `work` pass may defer
   polish, never correctness.
2. **Evidence over opinion.** Relay runs the tests itself and records the
   result; a test written for a fix must fail without the fix.
3. **Never invent or silently drop history.** Every override is explicit
   and is in the ledger, with the owner's note when one is given.
4. **Fail closed.** Missing or unreadable state stops work; recovery is an
   explicit command.
5. **The owner decides what to build.** Roles propose; gates wait for the
   owner, above all on matters of taste.
6. **Gates work from a phone.** Every gate can be read and decided in a few
   short lines; heavy building stays at the computer.

## How a task runs

- **The brief sizes the chain.** Its frontmatter gives `size`, `risky` and,
  for a fix or a feature, `deploy`. A `fix` starts at the Builder. A
  `feature` starts at the Designer and calls the Architect only when the
  structure changes. A `project` always runs the Designer and the Architect.
  `risky: yes` (auth, input, money, production data) adds the Tester and
  Security after the Reviewer. A brief that is missing a field, or can't be
  read, gets the full chain. `relay start` records the sizing, so editing
  the brief mid-run changes nothing.
- **Relay runs the tests.** After the Builder and after the Tester, `relay
  next` runs the profile's test command on the task's commit and records the
  outcome. A failed run goes back to the Builder. A passing run stops for
  the owner when the branch changed the test harness or fewer tests ran.
- **The base is a commit.** `relay start` confirms the task's base and
  records its commit; it is confirmed again before every role starts.
- **The Arbiter decides long loops and warnings.** A Reviewer, Tester or
  Security report with a high finding sends the task back to the Builder.
  Once 2 review loops have run on the current plan, or at any pass with
  warnings, the Arbiter rules move on, a targeted round, a re-plan or
  escalate to the owner; code stops for the owner before a move on could
  close the task, reach Ops, or pass an open Security high or a failing
  test run. Separately, `max_retries` (default 3) automatic loops back into
  the same phase stop the task.
- **Gates.** By default the owner approves the brief, the plan, whatever
  the Arbiter escalates, and anything that deploys. `relay waiting`, `relay
  card`, `relay show` and `relay decide` give each gate in short lines with
  every choice named, so it can be decided over Claude Code's Remote Control.
  `relay send-back --note` returns work, `relay note` leaves a note every
  later role reads, `relay override` moves a task by hand.
- **Deploys.** Ops runs only on tasks that deploy. It prepares the deploy,
  smoke test and rollback; the owner deploys and closes the task.
- **Prototypes.** `relay proto start|done` builds a timeboxed throwaway on a
  `proto/<name>` branch, outside the chain, with screenshots as evidence.
  `relay promote` turns it into a sized brief. Prototype code is never
  merged.

## Conventions every role follows

- Work happens on a branch of its own, in the checkout `relay start`
  records (often a worktree the session made). Never on the base branch.
- Read-only roles (Designer, Architect, Reviewer, Security, Ops, Arbiter) write only
  their own report; a hook enforces it.
- Each repo commits `.relay/profile.yaml` (commands, deploy targets, secret
  names only; see `docs/profile.md`) and may commit `.relay/threat-model.md`,
  which every role reads from the task's base branch.
- Findings are rated `high`, `medium` or `low` by one shared definition, and
  the highest rating decides the verdict.
- Every task is a `work`, `right` or `fast` pass. Tests and security are
  never deferred.
- Known flaws go in `docs/known-limits.md` in the same change that ships
  them.
- No secrets in output or commits, and no assistant references in commits.

## Where to read more

- `README.md`: install and a five-minute walk-through.
- `context/DECISIONS.md`: why things are the way they are.
- `docs/json-api.md`: the read API (`relay status --json`, `relay list --json`).
- `docs/profile.md`: the project profile.
- `docs/known-limits.md`: what is known to be wrong or unchecked.
- `DEPLOYMENT.md`: install, upgrade and rollback.
