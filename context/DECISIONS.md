# Relay decision log

Settled decisions and the reasons for them, grouped by topic. Reviewers and
roles: treat these as settled and raise a concern only with new evidence.
To change one, add the new decision and say which one it replaces.

## Shape of the system

- **Agents work, code enforces, the owner decides.** Gates, routing, the
  test run and the ledger are code; judgement is a role's; what to build is
  the owner's. A rule that only a prompt states is a weaker rule, and the
  docs say so where that is the case.
- **SQLite is the ledger.** One file holds tasks, steps, verdicts, test runs,
  notes and gate decisions. The CLI is the only writer. Reports stay as
  Markdown files in the task folder; the ledger points at them.
- **One central install, per-repo config.** The engine and ledger live in
  one install folder; each repo commits `.relay/profile.yaml` and an
  optional threat model. Relay works across many repos without copying
  itself into each one.
- **Python, standard library first.** The engine is Python with few
  dependencies and no build step.
- **Lean on Claude Code instead of rebuilding it.** Claude Code makes
  worktrees, runs sessions side by side, runs hooks around tools, keeps
  session state and gives phone access through Remote Control. Relay does
  not duplicate any of that: it records the checkout `relay start` is
  given, has no repo-wide lock, no config hooks and no session skills.
- **Project context lives in versioned files** (`context/`), not in any
  tool's memory, so every role and outside reviewer reads the same text.

## Roles

- **Seven roles: Designer, Architect, Builder, Reviewer, Tester, Security,
  Ops.** Designer decides what and why; Architect decides how. Penetration
  testing is part of Security. Ops runs only on tasks that deploy.
- **Each role prompt stands alone.** No shared house-rules file; the few
  lines every role shares are kept identical by a test.
- **Read-only roles are held read-only by Claude Code, not by words alone.**
  Their frontmatter limits tools and runs `hooks/only-write-reports.sh`
  before every write, so a write reaches only the role's own report or a
  temporary folder. They keep Bash, so the shell is held by the prompt; this
  is listed in `docs/known-limits.md`.
- **Role prompt size is held by a word ceiling and a line length**, not a
  line count. A line cap was gamed by packing rules into existing lines.
  Raising a ceiling is allowed when every new word earns its place.
- **Long commands go into a named script.** A shell command over about 10
  lines, any heredoc and any inline Python or Node is written to a file in
  a scratch folder and run in one line, because the owner often approves
  commands from a phone, where a long script cannot be judged.
- **Relay dispatches roles by their real names.** Under `--auto`, a fixed
  map (`AGENT_NAMES`) gives `claude --agent` each role's `name:`, and a test
  holds the map to `agents/*.md`.

## Sizing and routing

- **The brief sizes the chain.** `fix` starts at the Builder; `feature` runs
  the Designer and calls the Architect only when the structure changes;
  `project` always runs both. `risky: yes` adds the Tester and Security.
  Running seven roles on a one-line fix wasted time and attention.
- **Anything unclear gets more checking, never less.** A missing, garbled or
  unreadable field gives the full chain. The brief template defaults to
  `risky: yes`. Work that is not risky stops for the owner when Relay could
  run no tests, since no Tester will.
- **Sizing is recorded once.** `relay start` writes a `sized` event and
  routing reads that, so a brief edited mid-run (by a role, say) changes
  nothing.
- **Fix rounds end by consequence, with a tripwire.** A loop ends when the
  latest reports hold no high finding. After 2 fix rounds that still find a
  high, Relay stops and the owner decides. Without a cap, rounds chased
  ever smaller findings.
- **The retry limit counts only rounds nobody chose.** Loops Relay routes on
  its own count toward `max_retries` (default 3). A round the owner chose
  with `relay send-back` or `relay override` is never counted or refused.
- **A blocked task does not move.** `relay next` refuses it; only `relay
  override` moves it, or `relay send-back` when the retry limit stopped it,
  and `relay abort` closes it. In the read API a blocked
  task never reads as `ready`.
- **A pass with something still wrong always stops for the owner**
  (`approved_with_warnings`, `pass_with_concerns`, `findings`), even when no
  later phase follows. The recommended answer is to send it back; accepting
  is the exception. A send-back is recorded as its own event, never as an
  approval.

## Evidence

- **Relay runs the tests, not the role.** After the Builder and the Tester,
  `relay next` runs the profile's test command at the task's commit and
  records commit, command, exit code, duration and counts. Pass or fail is
  the exit code; counts come from JUnit XML when the profile names a file,
  and are never scraped from output. Relay's run wins over any report.
- **Tests must be able to fail.** A test added for a fix must fail on the
  code before the fix; the Builder commits it alone first, and may not drop
  tests or add skips.
- **A changed test command needs the owner.** The command is read from the
  base branch's profile too; when the task branch changes it, the owner
  confirms before the run counts.
- **One rating scale decides the verdict.** Reviewer, Tester and Security
  rate every finding high, medium or low by one definition, held identical
  across the three prompts by a test. High must be fixed before moving on;
  medium names the later task or person it would break; low is worth
  knowing. A finding that cannot name its consequence is low.
- **Ratings follow the repo's threat model.** `.relay/threat-model.md` says
  what is trusted and what the attack surface is. Roles read it from the
  base branch, because the task branch is not trusted. With no model, only
  the owner and Relay's installed files are trusted.
- **Roles prove their own work.** The Builder attacks its own round before
  reporting; "seen to fail" is the one accepted way to show a test fails for
  the right reason.
- **Manual test plans for what only a person can prove** (real devices,
  release builds, payments, stores), written from
  `templates/manual-test-plan.md` with one observable result per step.
- **Known flaws are written down where they ship.** A change that ships
  something known wrong adds it to `docs/known-limits.md`; the fix removes
  it. A chat or handoff is not the record.
- **Relay does not grade report prose.** Earlier versions checked that a
  report used the right headings and marks, and ran a separate auditor role
  every few fix rounds. Both were removed: checking words is a reviewer's
  job, and the 2-round tripwire stops a loop more simply. Relay keeps what
  code can prove: its own test run, the verdict table and the gates. The
  role prompts say so: the marks on lows (`Fix now`, `Record`) and the
  report headings are for the owner and the next role, and Relay reads none
  of them.

## The owner

- **Gates are decided in a few short lines.** `relay waiting`, `card`,
  `show` and `decide` print each gate in lines of at most 44 characters,
  with every choice named in words, so a gate can be decided over Remote
  Control from a phone. A local web board was built first and removed: the
  card and Remote Control cover the same need with less to maintain.
- **Owner notes reach every later role.** Notes left with an approval,
  a send-back or `relay note` are in the ledger and given to each later
  dispatch, oldest first.
- **The computer stays awake while a role runs.** Each dispatch holds the
  machine awake for up to `keep_awake_seconds` (two hours by default), and
  Relay reports sleeps that happened mid-phase.
- **The owner decides what to build.** The Designer may recommend not
  building something (`recommend-no-build`), which goes to the owner; it
  never quietly swaps in another idea.

## Deploys and prototypes

- **Ops has two modes, and Relay runs only one.** Prepare: build, local
  start and read-only checks; a smoke test for this change; a rollback that
  says what it cannot undo. The owner then deploys at the deploy gate.
  Execute mode is defined in the Ops prompt but Relay never dispatches it
  (`docs/known-limits.md`). Nothing yet checks the commands
  of a deploy against the approval, so Ops is told so plainly.
- **Profiles are strict.** Unknown keys are errors; secrets appear only as a
  name and where to find it; every command is one line run with `sh -c`
  from the repo root.
- **Prototypes are a separate, timeboxed path.** `relay proto` builds a
  throwaway on a `proto/` branch with no brief or roles, inside a timebox
  and guardrails (never production, demo data, no secrets), with screenshots
  as evidence. `relay promote` turns it into a sized brief. Prototype code
  is never merged, and `relay start` refuses a `proto/` branch. Fast demos
  and careful delivery need different processes; this keeps them apart.

## Process

- **Every task is a `work`, `right` or `fast` pass.** Readability blocks on
  `right` and `fast`; tests, security and correctness are never deferred.
- **Feature branch always; merge only after every phase passes.**
- **Outside review is a bounded spot check.** `tools/review_prompt.py` sends
  a document and this context to a stateless reviewer (an isolated headless
  session or one API call) for at most three ranked issues. It is used on
  prompts and major decisions, not as a gate on every report.
- **Done means shipped**: for this project's own work, merged to main and
  in real use. Relay's `done` phase only means a task's chain closed; Relay
  never merges anything.
