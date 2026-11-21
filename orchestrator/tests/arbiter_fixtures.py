"""Shared steps for tests that reach the Arbiter: playing the Arbiter by
writing its report, standing a task at a gate the way a `relay next` from
before the Arbiter left it, and recording passing test runs.

Every helper works on a ledger in a temp folder (the `config` fixture of
test_send_back.py). Nothing here imports a module the Arbiter added, so a
test that uses it fails on its assertions, not on an import, when it runs
against the code from before the Arbiter.
"""

import yaml

import commands
import db
import read_api
import routing
import run_state
import settings

# An escalation Relay can act on: a question, two choices and a recommendation.
ESCALATION = {"reason": "The owner must choose.",
              "question": "Ship the parser as it is, or fix the edge case first?",
              "choices": [{"says": "Fix the edge case first.", "does": "send-back"},
                          {"says": "Stop here.", "does": "abort"}],
              "recommended": 1}
TARGETED = {"reason": "One real problem remains.", "fix": ["Bound the parse."],
            "do_not_reopen": ["The report checks."]}
MOVE_ON = {"reason": "What is left is cosmetic.", "follow_ups": ["Reword the board line."],
           "known_flaws": ["The legacy reader is slow."]}
REPLAN = {"reason": "The same issue keeps coming back in new forms.", "replan_to": "architect"}
FIELDS_BY_VERDICT = {"escalate": ESCALATION, "targeted-round": TARGETED, "move-on": MOVE_ON,
                     "re-plan": REPLAN}


def latest_step(task_id: str, config: dict) -> dict:
    return read_api.status_payload(task_id, config)["latest_step"]


def finish_arbiter(task_id: str, config: dict, verdict, **fields) -> None:
    """Play the Arbiter: write the latest step's report with this verdict
    and these frontmatter fields (the usual ones for the verdict when none
    are given). A field given as None is left out."""
    latest = latest_step(task_id, config)
    assert latest["phase"] == "arbiter", f"the latest step is {latest['phase']}, not the Arbiter's"
    given = dict(FIELDS_BY_VERDICT.get(verdict, {})) if not fields else {}
    given.update(fields)
    frontmatter = {"task_id": task_id, "phase": "arbiter", "step": latest["step"],
                   "verdict": verdict}
    frontmatter.update({key: value for key, value in given.items() if value is not None})
    (settings.TASKS_DIR / task_id / latest["artifact"]).write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# Ruling\n\nBody.\n")


def stand_at_the_gate(task_id: str, config: dict) -> None:
    """Leave the latest step as a `relay next` from before the Arbiter left
    a gate it stopped at: its verdict read and recorded, nothing after it."""
    state = run_state.load_run_state(task_id, config)
    _, frontmatter = run_state.find_latest_artifact(task_id, config)
    run_state._record_verdict_in_history(state, frontmatter["phase"], frontmatter["step"],
                                         frontmatter["verdict"])
    run_state.save_run_state(task_id, state, config)


def passing_runs(monkeypatch) -> None:
    """Every "ready" step gets a passing test run recorded in the ledger, as
    a task with a test command does. (The walks have no working folder, so
    Relay would otherwise record "not run".) Like Relay's own, it records
    nothing on the files backend, which keeps no events."""
    def record_a_pass(task_id, state, phase, verdict, step, config):
        if (phase, verdict) not in routing.VERDICTS_RELAY_TESTS or not settings._sqlite_enabled(config):
            return None
        db.record_test_run(settings.get_db_connection(), task_id, phase=phase, step=step,
                           run={"outcome": "passed", "command": "make test", "exit_code": 0,
                                "commit": f"c{step:03d}", "duration_seconds": 1})
        return commands.latest_test_run(task_id, config, step)
    monkeypatch.setattr(commands, "ensure_test_run", record_a_pass)


def events_of(task_id: str, event_type: str) -> list[dict]:
    return [event for event in db.get_events(settings.get_db_connection(), task_id)
            if event["event_type"] == event_type]
