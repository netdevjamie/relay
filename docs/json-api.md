# Relay JSON read API

Three read commands print JSON for front ends (an editor pane, a terminal badge, a script):

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
- **Versioning.** Every document carries `schema` and `schema_version` (currently `3`). New keys may be added without a version change, so ignore keys you don't know. Renaming or removing a key, or changing what a value means, raises `schema_version`. The changes between versions are listed in a comment above `JSON_SCHEMA_VERSION` in `orchestrator/read_api.py`.
- `--json` is refused on commands that change state.

The shapes below are pinned by `orchestrator/tests/test_read_api.py`.

## `relay.list`

```json
{
  "schema": "relay.list",
  "schema_version": 3,
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

Tasks are ordered by `task_id`. `status` is one of `in_progress`, `completed`, `blocked`, `abandoned`. `repo` and the timestamps can be `null` on tasks migrated from before the ledger. `latest_step` is the newest step in brief (see `latest_step` below; a list row leaves out `recorded` and the file), or `null` if the task has none. `automatic_rounds` is set only when `gate_kind` is `retry_limit`: the number of automatic rounds in a row that ran into the stopped phase (what `relay.status` gives in `retries` for the gate's `next_phase`). It is above `max_retries` when the limit was lowered under the task. It is `null` everywhere else. `fix_rounds` is set only when the gate holds the gate key `fix_rounds` (below): the number of fix rounds the task has had. It is `null` everywhere else.

## `relay.gate-check`

```json
{
  "schema": "relay.gate-check",
  "schema_version": 3,
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
    "artifact_path": "/Users/you/Projects/.relay/tasks/add-export-button/01-designer.md"
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
| `approval` | Approve moving on to `next_phase`. `next_phase` is `done` when accepting the work closes the task: Security `findings` (or the legacy Pentest `findings`), or a Reviewer's `approved_with_warnings` on work that is not risky, on a task that does not deploy, where no Ops phase follows. | `relay override <task> <next_phase>` |
| `deploy_approval` | Ops says deploy-ready. Deploy, run the smoke test, then close the task. | `relay override <task> done` |
| `deploy_flag_missing` | The plan has no valid `deploy: yes\|no`. Add it to the plan, then run `relay next`. | `null` |
| `owner_decision` | There is no suggested next phase; the owner picks one or aborts. | `null` |
| `retry_limit` | Relay has routed the task back to `next_phase` on its own until that phase's count in `retries` reached `max_retries` (the count is higher when the limit was lowered afterwards, and 0 when the limit is 0), and will not do it again without the owner. The kind is the same before `relay next` has stopped the task (`status` is `in_progress`) and after (`status` is `blocked`). The owner sends it back for one more round (`send_back_command`), moves it with `relay override`, or aborts. | `null` |
| `fix_rounds` | A Reviewer's, Tester's or Security's report still holds a high after 2 fix rounds (every Builder step after the first counts, however the owner or Relay started it), so Relay stops for the owner instead of routing the task back to the Builder; it stops again after every later round whose report holds a high. `next_phase` is `builder`. The owner sends it back for another round (`send_back_command`, recommended), moves it with `relay override`, or aborts. | `null` |
| `blocked` | The task's `status` is `blocked` and the limit is not why: it is paused. The owner decides how to continue (`relay override`, `relay abort`). | `null` |

`kind`, `next_phase`, `approve_command` and `send_back_command` are `null` when they don't apply. `reason` is a sentence for people; don't parse it.


`fix_rounds` is in the gate only where the tripwire stopped the task: at a `fix_rounds` gate, and at a `retry_limit` gate when the limit and the tripwire stopped the same route (one stop, the limit's, whose `send_back_recommended` is then `true`). It is `{ "count", "lines" }`: `count` is the number of fix rounds, and `lines` is one sentence for people saying how many have run and that the latest report still asks for fixes (since version 3; before, a line per round named the highs its reports held).


`send_back_command` is set at `approval` and `owner_decision` gates, where there is finished work to accept or reject, and at a `retry_limit` or `fix_rounds` gate, where it runs one more round in `next_phase`. It is `null` everywhere else; `relay send-back` refuses anywhere it is not offered. At an `approval` gate the owner has two answers. `approve_command` accepts the work and moves on. `send_back_command` returns it for more work: to the Builder when the Reviewer, Tester or Security raised something, otherwise to the phase that wrote it (a brief to the Designer, a plan to the Architect). `send_back_recommended` is `true` when the verdict passed with something still wrong (`approved_with_warnings`, `pass_with_concerns`, `findings`); a front end should then offer sending back as the first choice. Sending back is recorded in the ledger as its own event, never as an approval. It is the owner's decision, so the limit never refuses it, it is not counted in `retries`, and it sets the count for the phase the work goes to back to 0. An optional note for the next agent goes on the command line: `relay send-back <task> --note "what to change"`.

A `blocked` task never reads as `ready`, whatever its latest verdict says, and `relay next` refuses it until the owner runs `relay override` or `relay abort`, or, when the limit stopped it, `relay send-back`. Only act on `ready`; treat every other state, and every error, as "do not proceed".

### `latest_step`

The newest step, or `null` if the task has none. `verdict` is `null` until the artifact carries a verdict that is valid for its phase. `recorded` is `true` once `relay next` has saved that verdict in the ledger. `started` is when the step was dispatched (its `history` timestamp), or `null` where the ledger has no time for it. `artifact` and `artifact_path` are `null` when no file is known for the step.

### `approvals`

Owner approvals recorded in the ledger, oldest first: `{ "phase", "next_phase", "method", "note", "timestamp" }`. `note` is what the owner said while approving, or `null`. `phase` is the phase the task was in when the owner approved; `method` is how: `override`, or, in a ledger written by an older version of Relay, `go-on` or `pause` at a process audit's gate (with `phase` `auditor`). Neither `go-on` nor `pause` sets a count back. This is a record of what the owner did; it does not yet tie an approval to a commit or a deploy target.

## `relay.status`

Everything about one task: the `gate`, `latest_step` and `approvals` objects described above, plus:

```json
{
  "schema": "relay.status",
  "schema_version": 3,
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
    "worktree_path": "/Users/you/Projects/worktrees/example-web-add-export-button",
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

`test_runs` lists the test runs Relay made itself, oldest first (`docs/profile.md`, Test evidence). `outcome` is `passed`, `failed`, `timed_out`, `uncommitted`, `profile_broken` or `not_run`. `counts` is `null` when the run wrote no JUnit report; `exit_code` and `duration_seconds` are `null` when nothing was run. `log` is a file name in the task folder. `command_differs_from_base` is `true` when the task's branch changed the test command or report path (or its base branch has no profile); a passing run then waits at an `approval` gate until the owner has accepted that command once. `base_command` is the test command the branch started from, or `null`.

One thing a gate cannot know in advance: while a Builder or Tester verdict is waiting to be picked up, the gate may read `ready` with the next phase the verdict points to. `relay next` runs the tests first, and a run that does not pass sends the task back to the Builder instead.

## `relay.profile`

`relay profile <repo-alias-or-path> --json` reads the repo's project profile (`docs/profile.md`). It needs no ledger.

```json
{
  "schema": "relay.profile",
  "schema_version": 3,
  "ok": true,
  "repo": "example-web",
  "repo_path": "/Users/you/Projects/example-web",
  "profile_path": "/Users/you/Projects/example-web/.relay/profile.yaml",
  "found": true,
  "valid": false,
  "errors": ["commands.test is required: the command that runs this repo's tests."],
  "warnings": [],
  "profile": { "schema_version": 1, "commands": {} }
}
```

`ok` is `true` whenever the question could be answered, including for a missing or invalid profile; read `valid`. The exit code is 0 only when `valid` is `true`. `profile` is the file's content as written, or `null` when it is missing or not a mapping. Don't act on `profile` unless `valid` is `true`.

## What a gate card reads from a report

What `relay card` lifts out of a report as written (`orchestrator/board_view.py` and `orchestrator/report_reader.py`; the Reviewer, Tester and Security prompts ask for it): the line `Summary for the owner: ...` under the title, a `## Rated findings` section holding a table of `# | Severity | Finding | Place` rows (severity `high`, `medium` or `low`) or the line `None.`, and, for Security, `Capped verdict: yes` or `no`. A report without the section is "not rated"; a row that breaks the format is listed under "Not rated" as written. A report quotes other text, so each is read strictly, and the whole file (quotes and code blocks included) is then looked over loosely for anything else that could be the same thing. The summary and capped lines are read between a `# Title` that is the report's first line and the next heading or code fence, and only when their opening words appear once in the whole file; otherwise the headline is Relay's own sentence, which says the summary line could not be read. The section is read from a `## Rated findings` line of its own starting in the first column, outside a code block; should there be more than one, the rows of all are read. Rows that are read are always listed. The card says there are no findings ("No findings at this gate.", the capped sentence "Security found nothing to fix") only when all of these hold: the one section holds the line `None.` and nothing else; exactly one line of the file begins with the words "rated findings"; no text in the file looks like a rated row; no code fence is left open; and the verdict in the ledger is the one the role gives when it found nothing (`approved`, `pass`, `secure`), or Security's `Capped verdict: yes` was read. Otherwise the card says what stopped it. When rows were read but the same look finds a second heading, an unread row or an open fence, the card adds "The card may not have read every finding in this report." under the list. A line ends at a newline and nowhere else.

## `relay.error`

```json
{
  "schema": "relay.error",
  "schema_version": 3,
  "ok": false,
  "error": { "code": "task_not_found", "message": "No run state for add-export-button." }
}
```

| `code` | Meaning |
| --- | --- |
| `task_not_found` | No task with that id. |
| `ledger_unavailable` | The ledger is missing or failed its integrity check. `message` says how to recover. |
| `repo_not_found` | `relay profile` was given something that is neither a registered alias nor a folder. |
| `internal_error` | Relay hit something it did not expect while answering. `message` names it. No answer was produced. |
