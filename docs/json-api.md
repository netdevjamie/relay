# Relay JSON read API

Three read commands print JSON for front ends (Claude Code pane, iTerm2 badges):

```
relay list --json
relay status <task-id> --json
relay gate-check <task-id> --json
```

Front ends display what these return and work nothing out themselves. To change a task, call the ordinary commands (`relay next`, `relay override`, `relay abort`).

## Rules

- **Read-only.** These commands never change the ledger or a task folder; with `--json` the ledger is opened read-only, so SQLite itself refuses a write. A verdict that is sitting in an artifact but not yet picked up by `relay next` is reported with `recorded: false`; it is not saved.
- **Stdout is one JSON document**, nothing else.
- **Exit code 0** means the answer is in the document. **Exit code 1** means the document is a `relay.error` (or, for `relay profile`, that the profile is missing or invalid). Exit code 2 is a usage error (message on stderr, no JSON).
- **Fail closed.** If the ledger is missing or corrupt the answer is a `ledger_unavailable` error, never a guess. A front end that enforces anything must treat every error as "not approved".
- **Versioning.** Every document carries `schema` and `schema_version` (currently `4`). New keys may be added without a version change, so ignore keys you don't know. Renaming or removing a key, or changing what a value means, raises `schema_version`. The table under "Versions" says what each version changed.
- `--json` is refused on commands that change state.

### Versions

| Version | What changed |
| --- | --- |
| 1 | The first shapes. |
| 2 | Two meanings, no key. `retries` counts automatic rounds only, where it counted every loop back, send-backs included. A task stopped at the limit reads as a `retry_limit` gate that offers `send_back_command`, where it read `blocked` with none. |
| 3 | Keys removed: no `audits` on a status, no `audit` on a list row or a gate, no `process_audit` gate kind, no `relay_found` on a gate. `fix_rounds.lines` is one sentence. |
| 4 | The Arbiter. A warning verdict (`approved_with_warnings`, `pass_with_concerns`, `findings`) goes to the Arbiter, where it stopped at an `approval` gate for the owner to decide the medium. A high past the tripwire goes to the Arbiter, where it stopped at a `fix_rounds` gate; a `fix_rounds` gate is only one an older Relay left standing. No stop sets `send_back_recommended` by default. `phase` and `next_phase` may be `arbiter`. Three gate kinds are new (`arbiter_escalation`, `arbiter_needs_owner`, `arbiter_unreadable`). A gate may carry `arbiter` and `review_loops`. A status carries `builder_steps`, `arbiter_rulings` and `follow_ups`. |

Keys added within version 4, with no version change: `sizing`, `sizing_history`, `risk_hint` and `sizing_judgments` on a status (the sizing hint and `relay resize`), `time_limit_stops` (the time limit on a role), and the harness and Seen to fail keys on a test run (below).

The shapes below are pinned by `orchestrator/tests/test_read_api.py`.

## `relay.list`

```json
{
  "schema": "relay.list",
  "schema_version": 4,
  "ok": true,
  "tasks": [
    {
      "task_id": "add-export-button",
      "repo": "example-web",
      "status": "in_progress",
      "current_phase": "designer",
      "deploy_intent": false,
      "created": "2026-10-03T14:00:00Z",
      "updated": "2026-10-03T14:05:00Z",
      "gate_state": "waiting_on_owner",
      "gate_kind": "approval",
      "automatic_rounds": null,
      "fix_rounds": null,
      "latest_step": {
        "step": 1,
        "phase": "designer",
        "verdict": "ready-for-architect",
        "started": "2026-10-03T14:00:00Z"
      }
    }
  ]
}
```

Tasks are ordered by `task_id`. `status` is one of `in_progress`, `completed`, `blocked`, `abandoned`. `repo` and the timestamps can be `null` on tasks migrated from before the ledger. `latest_step` is the newest step in brief (see `latest_step` below; a list row leaves out `recorded` and the file), or `null` if the task has none. `automatic_rounds` is set only when `gate_kind` is `retry_limit`: the number of automatic rounds in a row that ran into the stopped phase (what `relay.status` gives in `retries` for the gate's `next_phase`). It is above `max_retries` when the limit was lowered under the task. It is `null` everywhere else. (A new key, so no version change.) `fix_rounds` is set only when the gate holds the list of highs per fix round (the gate key `fix_rounds`, below): the number of fix rounds the task has had. It is `null` everywhere else. (A new key, so no version change.) `time_limit` is the gate key `time_limit` (below) when `gate_kind` is `time_limit`, and `null` everywhere else. (A new key, so no version change.)

## `relay.gate-check`

```json
{
  "schema": "relay.gate-check",
  "schema_version": 4,
  "ok": true,
  "task_id": "add-export-button",
  "status": "in_progress",
  "current_phase": "designer",
  "latest_step": {
    "step": 1,
    "phase": "designer",
    "verdict": "ready-for-architect",
    "recorded": false,
    "started": "2026-10-03T14:00:00Z",
    "artifact": "01-designer.md",
    "artifact_path": "/home/you/Projects/.relay/tasks/add-export-button/01-designer.md"
  },
  "gate": {
    "state": "waiting_on_owner",
    "kind": "approval",
    "next_phase": "architect",
    "reason": "designer verdict: ready-for-architect, requires human approval to proceed",
    "approve_command": "relay override add-export-button architect",
    "send_back_command": "relay send-back add-export-button",
    "send_back_recommended": false
  },
  "approvals": []
}
```

### `gate`

The gate says whether the task can move on. It is worked out by the same routing code `relay next` runs, so the two agree: where the gate says `waiting_on_owner`, `relay next` stops too.

| `state` | Meaning |
| --- | --- |
| `waiting_on_agent` | The current step has no valid verdict yet. Nothing for the owner to do. |
| `waiting_on_owner` | `relay next` would stop for the owner. `kind` says why. |
| `ready` | `relay next` would move on without the owner. `next_phase` is the phase it would start, or `done` if the task would complete. |
| `closed` | The task is completed or abandoned. |

| `kind` (only when `waiting_on_owner`) | Meaning | `approve_command` |
| --- | --- | --- |
| `approval` | Approve moving on to `next_phase`. `next_phase` is `done` when accepting the work closes the task: Security `findings` (or the legacy Pentest `findings`) on a task that does not deploy, where no Ops phase follows. | `relay override <task> <next_phase>` |
| `deploy_approval` | Ops says deploy-ready. Deploy, run the smoke test, then close the task. | `relay override <task> done` |
| `deploy_flag_missing` | The plan has no valid `deploy: yes\|no`. Add it to the plan, then run `relay next`. | `null` |
| `owner_decision` | There is no suggested next phase; the owner picks one or aborts. | `null` |
| `retry_limit` | Relay has routed the task back to `next_phase` on its own (an Arbiter's targeted round or re-plan counts too) until that phase's count in `retries` reached `max_retries` (the count is higher when the limit was lowered afterwards, and 0 when the limit is 0), and will not do it again without the owner. The kind is the same before `relay next` has stopped the task (`status` is `in_progress`) and after (`status` is `blocked`). The owner sends it back for one more round (`send_back_command`), moves it with `relay override`, or aborts. | `null` |
| `fix_rounds` | Since version 4, only a gate left standing from before the Arbiter: a Reviewer's, Tester's or Security's high that an older `relay next` stopped at after 2 fix rounds (every Builder step after the first). `next_phase` is `builder`. The owner sends it back for another round (`send_back_command`, no longer recommended), moves it with `relay override`, or aborts. A gate left standing at a warning verdict reads as `approval`, with `send_back_recommended` `true`, as before. | `null` |
| `arbiter_escalation` | The Arbiter asks the owner to decide. `gate.arbiter` holds its question, its choices (each says what Relay runs: send-back, move-to a phase, abort, note) and the one it recommends. `gate.arbiter.needs_owner_because` holds Relay's own warnings, in Relay's words: Security's latest report still holds a high (a Security high since the latest Builder step), Security's latest report holds a high, and Security has not run since the fix (a Builder step came after Security's latest report, which is a high; given only when a choice would close the task or reach Ops, since those skip Security), Relay's latest test run did not pass, Relay's reason that the passing run at the report the Arbiter ruled on awaits the owner's confirmation (a changed test command or report path, changed or unchecked harness files, or fewer tests run, in the words of the approval gate; given only when a choice moves past that report, to a later phase or done, since such a choice starts no Builder step and is not tested again before it), and each choice that would close the task or reach Ops. While any of the first three holds, `recommends` is `null` where the Arbiter recommended a choice that closes or reaches Ops; while the confirmation reason is given, `recommends` is `null` where the Arbiter recommended a choice that moves past that report. Taking such a choice once the reason is given records the owner's acceptance (`test_command_accepted`), as approving at the approval gate does; no other choice or override at an escalation records one. `next_phase` is `null`. | `null` |
| `arbiter_needs_owner` | The Arbiter's ruling needs the owner's yes: a move-on while Security has reported a high since the latest Builder step or Relay's latest test run did not pass, a move-on that would close the task (`next_phase` `done`) or reach Ops (`next_phase` `ops`), or a second re-plan on the task (`next_phase` the re-plan's target). `gate.arbiter.needs_owner_because` says which, in Relay's words. | `relay override <task> <next_phase>` |
| `arbiter_unreadable` | Relay could not act on the Arbiter's report: missing, no readable frontmatter, a verdict outside the calls open in its situation, a re-plan target other than the Architect or Designer, a targeted round with no fix list, or an escalation missing its parts. `reason` says which in one line. Nothing moved. For a missing report this gate is shown only once `relay next` has looked; before that the step reads `waiting_on_agent`. | `null` |
| `risk_unanswered` | The Reviewer had to answer the sizing hint and did not: the hint matched on a task sized `risky: no`, and its report's frontmatter has no `sizing: raise` or `sizing: keep`, an answer Relay cannot read, a keep with no `sizing_reason`, a raise that names nothing Relay can raise, or a raise that leaves the task `risky: no` (a raise of the size alone). `reason` says which. `next_phase` is where the report would otherwise have gone. The owner approves, sends it back, or re-sizes the task with `relay resize` and runs `relay next`. (A new kind, so no version change.) | `relay override <task> <next_phase>` |
| `time_limit` | A role Relay started under `--auto` ran past its time limit and was stopped (2 hours unless Relay's config says otherwise). `status` is `blocked`. Nothing moves past the step, even when the stopped role left a report with a verdict. `next_phase` is the stopped phase. The owner starts it again (`relay override <task> <next_phase>`), moves the task with `relay override`, or aborts. | `null` |
| `blocked` | The task's `status` is `blocked` and neither limit is why: it is paused. The owner decides how to continue (`relay override`, `relay abort`). | `null` |

`kind`, `next_phase`, `approve_command` and `send_back_command` are `null` when they don't apply. `reason` is a sentence for people; don't parse it.


`fix_rounds` is in the gate only at a `fix_rounds` gate left standing from before the Arbiter (since version 4; before, also at a `retry_limit` gate the tripwire shared). It is `{ "count", "lines" }`: `count` is the number of fix rounds, and `lines` is one sentence for people saying how many have run and that the latest report still asks for fixes (since version 3; before, a line per round named the highs its reports held).


`send_back_command` is set at `approval`, `owner_decision`, `risk_unanswered` and the three `arbiter_` gates, where there is finished work to accept or reject (from an Arbiter's step it goes to the Builder), and at a `retry_limit` or `fix_rounds` gate, where it runs one more round in `next_phase`. It is `null` everywhere else; `relay send-back` refuses anywhere it is not offered. At an `approval` gate the owner has two answers. `approve_command` accepts the work and moves on. `send_back_command` returns it for more work: to the Builder when the Reviewer, Tester or Security raised something, otherwise to the phase that wrote it (a brief to the Designer, a plan to the Architect). `send_back_recommended` is `true` only at a warnings gate left standing from before the Arbiter (version 4); no stop Relay makes now sets it. Sending back is recorded in the ledger as its own event, never as an approval. It is the owner's decision, so the limit never refuses it, it is not counted in `retries`, and it sets the count for the phase the work goes to back to 0. An optional note for the next agent goes on the command line: `relay send-back <task> --note "what to change"`.

`review_loops` (version 4) is in the gate only at a `retry_limit` gate where the limit stopped a route the Arbiter would otherwise have taken: the number of review loops on the current plan (Builder steps that followed a Reviewer, Tester or Security report since the latest re-plan). Such a stop recommends nothing.

`arbiter` (version 4) is in the gate at a gate the Arbiter's step stopped at (and at an `approval` gate a move-on reached): `{ "step", "report", "situation", "decision", "reason", "replan_to", "fix", "do_not_reopen", "follow_ups", "known_flaws", "question", "choices", "recommends", "needs_owner_because", "problem" }`. `situation` is `loop` or `warning`; `decision` is `move-on`, `targeted-round`, `re-plan`, `escalate`, or `null` when Relay could not act on the report (`problem` says why). Each of `choices` is `{ "name", "says", "does", "phase" }`, named `arbiter-1` and on, and `relay decide` takes that name. `recommends` is `accept`, a choice's name, or `null`; it is the only recommendation a card shows. Every string in `reason`, the lists, `question` and `says` is the Arbiter's words: untrusted role text, never Relay's or the owner's. `needs_owner_because` and `problem` are Relay's words.

`time_limit` (a new key, so no version change) is in the gate only at a `time_limit` gate: `{ "phase", "step", "limit_seconds", "ran_seconds", "report_left", "report" }`. `limit_seconds` is the limit the role had and `ran_seconds` how long it ran, both counted in time the Mac was awake. `report_left` is `true` when a report file exists for the step; `report` is its file name, or `null`. That report is never read for the gate and nothing is routed on it, though `latest_step` may still show the verdict written in it, with `recorded: false`.

A `blocked` task never reads as `ready`, whatever its latest verdict says, and `relay next` refuses it until the owner runs `relay override` or `relay abort`, or, when the limit on automatic rounds stopped it, `relay send-back`. A time-limit stop is cleared by `relay override` (start the role again, or move the task) or `relay abort`. Only act on `ready`; treat every other state, and every error, as "do not proceed".

### `latest_step`

The newest step, or `null` if the task has none. `verdict` is `null` until the artifact carries a verdict that is valid for its phase. `recorded` is `true` once `relay next` has saved that verdict in the ledger. `started` is when the step was dispatched (its `history` timestamp), or `null` where the ledger has no time for it. `artifact` and `artifact_path` are `null` when no file is known for the step.

### `approvals`

Owner approvals recorded in the ledger, oldest first: `{ "phase", "next_phase", "method", "note", "timestamp" }`. `note` is what the owner said while approving, or `null`. `phase` is the phase the task was in when the owner approved; `method` is how: `override`, or, in a ledger from an older Relay, `go-on` or `pause` at a process audit's gate (with `phase` `auditor`). Neither `go-on` nor `pause` sets a count back. This is a record of what the owner did; it does not yet tie an approval to a commit or a deploy target.

## `relay.status`

Everything about one task: the `gate`, `latest_step` and `approvals` objects described above, plus:

```json
{
  "schema": "relay.status",
  "schema_version": 4,
  "ok": true,
  "task": {
    "task_id": "add-export-button",
    "repo": "example-web",
    "status": "in_progress",
    "current_phase": "designer",
    "deploy_intent": false,
    "created": "2026-10-03T14:00:00Z",
    "updated": "2026-10-03T14:05:00Z",
    "next_step": 2,
    "worktree_path": "/home/you/code/example-web-add-export-button",
    "branch": "add-export-button",
    "base": "main"
  },
  "retries": { "builder": 0, "reviewer": 0 },
  "max_retries": 3,
  "history": [
    { "step": 1, "phase": "designer", "verdict": null, "timestamp": "2026-10-03T14:00:00Z",
      "artifact": "01-designer.md" }
  ],
  "latest_step": {},
  "gate": {},
  "approvals": [],
  "owner_notes": [
    {
      "kind": "approval",
      "phase": "designer",
      "step": null,
      "next_phase": "architect",
      "method": "override",
      "note": "Keep 5 contacts, not 3.",
      "timestamp": "2026-10-05T12:00:00Z"
    }
  ],
  "time_limit_stops": [],
  "interruptions": [
    {
      "phase": "builder",
      "step": 3,
      "noticed": "2026-10-05T12:00:00Z",
      "sleeps": 1,
      "asleep_seconds": 5655,
      "first_slept_at": "2026-10-05T09:03:13Z",
      "last_slept_at": "2026-10-05T09:03:13Z"
    }
  ],
  "test_runs": [
    {
      "phase": "builder",
      "step": 3,
      "timestamp": "2026-10-04T14:00:00Z",
      "outcome": "passed",
      "reason": null,
      "commit": "0123abcd...",
      "command": "npm test",
      "test_report": "test-results/junit.xml",
      "command_differs_from_base": false,
      "base_command": "npm test",
      "exit_code": 0,
      "duration_seconds": 12.4,
      "counts": { "tests": 120, "failures": 0, "errors": 0, "skipped": 2 },
      "log": "03-test-run.log"
    }
  ]
}
```

`history` has one entry per step, oldest first, and shows only verdicts already recorded in the ledger. `artifact` is the file name recorded for the step in the task folder, or `null` when none is known. The name is recorded when the step is dispatched, so the file may not exist yet. `retries` counts automatic rounds: for each phase, how many times in a row Relay has routed the task back to it on its own (a Reviewer's `changes_requested`, a failed test run) since the owner last sent work back to that phase or overrode into it. A round the owner chose is not counted and sets that phase's count to 0. So does `relay override` on a task the limit stopped, for the stopped phase, whichever phase the override moves the task to. `max_retries` is the limit on that count. `builder` and `reviewer` are always present.


`owner_notes` lists what the owner has said on the task, oldest first. `kind` is `approval` (said while approving with `relay override ... --note`; `next_phase` is where the task went), `sent_back` (said with `relay send-back ... --note`; `next_phase` is who got the work back) or `note` (left with `relay note`, which moves nothing; `next_phase` is `null`). `method` is the approval's method (`override`, `go-on`, `pause`), `null` for the other kinds. Approvals and send-backs without a note are not listed here. Every agent dispatched afterwards is given the `approval` and `note` entries. A `sent_back` note is the instruction for that one round of fixes: the agent doing the fix is given it, later agents are not. One note is at most 4,000 characters.

`interruptions` has at most one entry per step: the machine went to sleep between the step being dispatched and its artifact being written. `sleeps` is how many times, `asleep_seconds` the total until each next full wake, and `noticed` when `relay next` recorded it. A sleep after the artifact was written is not the step's and is not listed. Read from the system's power log, which keeps about a week; empty where that cannot be read, which means unknown, not "never slept".

`builder_steps` (version 4) is the task's Builder steps by cause: `{ "replan_step", "review_loops_since_replan", "task", "since_replan", "steps" }`. `task` and `since_replan` each count `total`, `first_build`, `review_loop`, `failed_test_run`, `after_replan` and `owner_elsewhere`, with `review_loop_by_role` (`reviewer`, `tester`, `security`) and `review_loop_by_starter` (`relay`, `arbiter`, `owner`). `since_replan` counts after the latest re-plan, or after the first build when there has been none. `steps` is each Builder step's `{ "step", "cause", "role", "started_by" }`. Worked out from the history and the recorded test runs every time, so an older task has it too.

`arbiter_rulings` (version 4) lists every Arbiter ruling Relay acted on and every report it could not, oldest first: `{ "step", "report", "situation", "decision", "reason", "replan_to", "routed", "trigger", "problem", "timestamp" }`. `routed` is `true` when Relay moved the task on it; `trigger` is the report it ruled on (`{ "step", "phase", "verdict" }`). A ruling is never an approval: `approvals` stays the owner's.

`follow_ups` (version 4) lists what was accepted and logged on the task, oldest first: `{ "step", "report", "source", "kind", "text", "severity", "timestamp" }`. `source` is `arbiter` (a routed ruling's `follow_up` and `known_flaw` items) or `owner` (findings the owner accepted and logged with `relay decide ... accept-and-log`, `kind` `finding` or the Arbiter's kinds). Every later agent is given them: the owner's as accepted and logged, the Arbiter's apart, as recorded by the Arbiter and not accepted by the owner, never with a request not to raise them. `text` is role text. The files backend keeps no events, so these three lists are empty there.

`sizing`, `sizing_history`, `risk_hint` and `sizing_judgments` (new keys within version 4) say how big and how risky the task is, how that changed, and why:

```json
{
  "sizing": { "size": "fix", "risky": true, "deploy": false, "note": null },
  "sizing_history": [
    { "size": "fix", "risky": false, "deploy": false, "from": null, "reason": null,
      "trigger": { "by": "start", "step": null }, "timestamp": "2026-11-20T10:00:00Z" },
    { "size": "fix", "risky": true, "deploy": false, "from": { "size": "fix", "risky": false },
      "reason": "It changes how sessions are kept.", "trigger": { "by": "reviewer", "step": 2 },
      "timestamp": "2026-11-20T11:00:00Z" }
  ],
  "risk_hint": { "step": 1, "timestamp": "2026-11-20T10:30:00Z", "available": true, "reason": null,
                 "note": null, "fork": "0123...", "commit": "4567...", "changed": 3,
                 "categories": [ { "category": "auth", "count": 1, "paths": ["auth/session.py"] } ] },
  "sizing_judgments": [
    { "phase": "reviewer", "step": 2, "timestamp": "2026-11-20T11:00:00Z", "by": "reviewer",
      "answer": "raise", "reason": "It changes how sessions are kept.", "refused": [], "lowered": [],
      "unanswered": null, "hint": { "auth": 1 } }
  ]
}
```

`sizing` is the sizing routing uses now: the latest recorded one. `sizing_history` is the start, then every change, oldest first. `trigger.by` is `start`, `reviewer` or `arbiter` (a raise in that role's report, at `trigger.step`, set by Relay and never read from the report), or `owner` (`relay resize`, at the task's latest step). `from` is the size and risk before the change. A recorded change Relay cannot read is one entry `{ "unreadable": true }`. On the files backend the history is the brief's sizing, then each raise read back from the Reviewer's and Arbiter's reports, with `timestamp` `null`.

`risk_hint` is the newest sizing hint, worked out after a Builder step that reported ready, or `null`: which risk categories (`auth`, `input`, `payments`, `schema`, `production_data`, `secrets`, `deploy`) the paths the branch changed since it left its base look like they touch. `available` is `false` when it could not be worked out, with `reason`; `note` says why the base profile's `risk_paths` were not used (the defaults still were). Each category keeps at most 20 paths, each cut to 300 characters, and counts all of them. **The paths are carried raw, as the branch's text**: Relay does not clean them for the JSON, so they may hold control characters or anything else a file name can. A front end must treat them as untrusted and make them safe to show itself. Relay's own words (the prompts, `relay status`, the card) show them only through its printable handling.

`sizing_judgments` lists what each Reviewer's or Arbiter's report said of the sizing, where it answered (`raise` or `keep`), asked for something Relay refused (`refused`: a value outside the allowed words; `lowered`: a lowering, which only the owner may make), or did not answer a hint it had to (`unanswered`, Relay's words). `reason` is the role's words. `hint` is the categories and counts of the hint it answered. The files backend keeps no events, so `risk_hint` is `null` and `sizing_judgments` is empty there.

`time_limit_stops` lists every time a role's time limit stopped it, oldest first: `{ "phase", "step", "limit_seconds", "ran_seconds", "stopped_at" }`. The files backend keeps no events, so it is always empty there. (A new key within version 4.)

`test_runs` lists the test runs Relay made itself, oldest first (`docs/profile.md`, Test evidence). `outcome` is `passed`, `failed`, `timed_out`, `uncommitted`, `profile_broken` or `not_run`. `counts` is `null` when the run wrote no JUnit report; `exit_code` and `duration_seconds` are `null` when nothing was run. `log` is a file name in the task folder. `command_differs_from_base` is `true` when the task's branch changed the test command or report path (or its base branch has no profile); a passing run then waits at an `approval` gate until the owner has accepted that command once. `base_command` is the test command the branch started from, or `null`. A `failed` run can have `exit_code` 0: it exited 0 but ran 0 tests, or did not write the report its profile names; `reason` says which.

Runs recorded by a Relay with the harness checks also have the keys below, after the ones above. A run recorded by an older Relay has none of them, which means not flagged and not checked.

- `tests_run`: the report's tests minus its skipped, or `null` with no readable report. `counts_unchecked` says why counts were not checked for 0 or fewer tests (the profile names no report), or is `null`.
- `harness_changed`: harness files the branch changed against its base (at most 50), `[]` when none. `harness_digest` identifies their content at this commit, or `null`.
- `previous_tests_run`: tests run by the latest earlier run of the task that had counts. `base_commit` is where the branch left its base; `base_tests_run` the tests run there, or `null` with `base_count_unknown` saying why.
- `tests_drop`: `null`, or `{"fewer", "reference", "before", "now"}` for the larger drop in tests run, with `reference` `base` or `previous run`. A passing run with harness files changed or a drop waits at an `approval` gate, as a changed command does, until the owner has accepted that harness change or that count.
- `clean_checkout`: `true` when the run used a fresh checkout. `ignored_present` lists top-level ignored paths present in the worktree when it did not (`null` in clean mode).
- `seen_to_fail`: `null`, or the result of running the branch's new or changed test files at the base: `status` (`checked` or `could_not_check`), `reason`, the counts `failed`, `passed`, `did_not_run` and `could_not_check`, `passed_names`, `cases` (each `{"test", "result", "why"}`, at most 200) and `base_commit`. It never changes the gate.
- `base_log` and `seen_to_fail_log`: file names in the task folder for those extra runs' output, or `null`.
- `harness_unchecked`: why Relay could not tell which harness files the branch changed (the commit where it left its base cannot be found, as for a branch with no history shared with it, or git could not list its changes, or the profile at the base commit Relay confirmed for the task could not be read), or `null`. A passing run with it set waits at an `approval` gate until the owner has accepted that commit.

One thing a gate cannot know in advance: while a Builder or Tester verdict is waiting to be picked up, the gate may read `ready` with the next phase the verdict points to. `relay next` runs the tests first, and a run that does not pass sends the task back to the Builder instead.

## `relay.profile`

`relay profile <repo-alias-or-path> --json` reads the repo's project profile (`docs/profile.md`). It needs no ledger.

```json
{
  "schema": "relay.profile",
  "schema_version": 4,
  "ok": true,
  "repo": "example-web",
  "repo_path": "/home/you/code/example-web",
  "profile_path": "/home/you/code/example-web/.relay/profile.yaml",
  "found": true,
  "valid": false,
  "errors": ["commands.test is required: the command that runs this repo's tests."],
  "warnings": [],
  "profile": { "schema_version": 1, "commands": {} }
}
```

`ok` is `true` whenever the question could be answered, including for a missing or invalid profile; read `valid`. The exit code is 0 only when `valid` is `true`. `profile` is the file's content as written, or `null` when it is missing or not a mapping. Don't act on `profile` unless `valid` is `true`.

## What a gate card reads from a report

What `relay card` lifts out of a report as written (`orchestrator/board_view.py` and `orchestrator/report_reader.py`; the Reviewer, Tester and Security prompts ask for it): the line `Summary for the owner: ...` under the title, a `## Rated findings` section holding a table of `# | Severity | Finding | Place` rows (severity `high`, `medium` or `low`) or the line `None.`, and, for Security, `Capped verdict: yes` or `no`. A report without the section is "not rated"; a row that breaks the format is listed under "Not rated" as written. A report quotes other text, so each is read strictly, and the whole file (quotes and code blocks included) is then looked over loosely for anything else that could be the same thing. The summary and capped lines are read between a `# Title` that is the report's first line and the next heading or code fence, and only when their opening words appear once in the whole file; otherwise the headline is Relay's own sentence, which says the summary line could not be read. The section is read from a `## Rated findings` line of its own starting in the first column, outside a code block; should there be more than one, the rows of all are read. Rows that are read are always listed. The card says there are no findings ("No findings at this gate. The report is below.", the capped sentence "Security found nothing to fix") only when all of these hold: the one section holds the line `None.` and nothing else; exactly one line of the file begins with the words "rated findings"; no text in the file looks like a rated row; no code fence is left open; and the verdict in the ledger is the one the role gives when it found nothing (`approved`, `pass`, `secure`), or Security's `Capped verdict: yes` was read. Otherwise the card says what stopped it. When rows were read but the same look finds a second heading, an unread row or an open fence, the card adds "The board may not have read every finding in this report." under the list. A line ends at a newline and nowhere else.

## `relay.error`

```json
{
  "schema": "relay.error",
  "schema_version": 4,
  "ok": false,
  "error": { "code": "task_not_found", "message": "No run state for add-export-button." }
}
```

| `code` | Meaning |
| --- | --- |
| `task_not_found` | No task with that id. |
| `ledger_unavailable` | The ledger is missing or failed its integrity check, or an event the answer needs has a payload Relay cannot read as JSON (only a hand edit makes one). `message` says how to recover. |
| `repo_not_found` | `relay profile` was given something that is neither a registered alias nor a folder. |
| `internal_error` | Relay hit something it did not expect while answering. `message` names it. No answer was produced. |
