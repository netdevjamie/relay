"""Relay's own test runs in the ledger: running the repo's tests for a
step (via evidence.py), reading recorded runs back, and whether the owner
has accepted a changed test command.
"""

from pathlib import Path

import db
import evidence
import settings
from settings import _sqlite_enabled, _task_cwd, get_db_connection
from routing import VERDICTS_RELAY_TESTS


def read_test_runs(task_id: str, config: dict) -> list[dict]:
    """Test runs Relay made for this task, oldest first. The files backend
    keeps no events, so it has none."""
    if not _sqlite_enabled(config):
        return []
    return [{"phase": event["phase"], "step": event["step"], "timestamp": event["timestamp"],
             **(event.get("payload") or {})}
            for event in db.get_events(get_db_connection(), task_id)
            if event["event_type"] == db.EVENT_TEST_RUN]


def latest_test_run(task_id: str, config: dict, step: int | None) -> dict | None:
    """The newest test run recorded for one step. A run recorded without a
    step number only ever matches a lookup without one."""
    runs = [run for run in read_test_runs(task_id, config) if run["step"] == step]
    return runs[-1] if runs else None


def is_test_command_confirmed(task_id: str, config: dict, test_run: dict | None) -> bool:
    """True when the owner has accepted this exact test command and report
    path for this task. Only the acceptance recorded at the gate that asked
    counts (see _accept_test_command); no other approval stands in for it.
    One acceptance covers the rest of the task; a new command asks again."""
    if not test_run or not _sqlite_enabled(config):
        return False
    setup = (test_run.get("command"), test_run.get("test_report"))
    return any(event["event_type"] == db.EVENT_TEST_COMMAND_ACCEPTED
               and ((event.get("payload") or {}).get("command"),
                    (event.get("payload") or {}).get("test_report")) == setup
               for event in db.get_events(get_db_connection(), task_id))


def _test_command_awaits_owner(task_id: str, config: dict, phase: str, verdict: str,
                               test_run: dict | None) -> bool:
    """True when a passing run is waiting for the owner to accept a test
    command the base branch does not have."""
    return bool(test_run and (phase, verdict) in VERDICTS_RELAY_TESTS
                and test_run.get("outcome") == evidence.OUTCOME_PASSED
                and test_run.get("command_differs_from_base")
                and not is_test_command_confirmed(task_id, config, test_run))


def ensure_test_run(task_id: str, state: dict, phase: str, verdict: str, step,
                    config: dict) -> dict | None:
    """Run the repo's tests for a step that just reported "ready", record
    the run in the ledger and return it. None when this verdict needs no
    run, or on the files backend (which keeps no events).

    A task with no working directory gets a `not_run` record, so the gap is
    visible. A run already recorded for this step at the commit the worktree
    is on is reused, so running `relay next` twice does not test twice."""
    if (phase, verdict) not in VERDICTS_RELAY_TESTS or not _sqlite_enabled(config):
        return None
    step = step if isinstance(step, int) else None
    recorded = latest_test_run(task_id, config, step)
    work_dir = _task_cwd(state)
    if not work_dir or not Path(work_dir).is_dir():
        if recorded and recorded.get("outcome") == evidence.OUTCOME_NOT_RUN:
            return recorded
        run = evidence.not_run("The task has no working directory, so Relay had nowhere to run tests.")
    else:
        worktree = Path(work_dir)
        conclusive = (evidence.OUTCOME_PASSED, evidence.OUTCOME_FAILED, evidence.OUTCOME_TIMED_OUT)
        if (recorded and recorded.get("outcome") in conclusive
                and recorded.get("commit") == evidence.head_commit(worktree)
                and not evidence.uncommitted_changes(worktree)):
            return recorded
        print(f"  Running the repo's tests (Relay's own run)...")
        log_name = f"{step:02d}-test-run.log" if step is not None else "test-run.log"
        run = evidence.run_tests(worktree, settings.TASKS_DIR / task_id / log_name, base=state.get("base"),
                                 timeout_seconds=config.get("test_timeout_seconds",
                                                            evidence.DEFAULT_TIMEOUT_SECONDS))
    db.record_test_run(get_db_connection(), task_id, phase=phase, step=step, run=run)
    return latest_test_run(task_id, config, step)


def _test_run_prompt(task_id: str, test_run: dict | None, next_phase: str) -> str:
    """What the next agent is told about Relay's recorded test run."""
    if not test_run:
        return ""
    text = f"\n\nRelay's recorded test run: {evidence.summary(test_run)}"
    log = settings.TASKS_DIR / task_id / test_run["log"] if test_run.get("log") else None
    if log:
        text += f"\nFull output: {log}"
    if next_phase == "builder" and test_run.get("outcome") in evidence.FAILING_OUTCOMES:
        text += ("\n\nThis step exists because that run did not pass. Relay's run is the "
                 "record: make the profile's test command pass at a commit, with nothing "
                 "left uncommitted, before reporting ready again.")
        if test_run.get("reason"):
            text += f"\n{test_run['reason']}"
        if log:
            text += f"\n\nEnd of the output:\n{evidence.log_tail(log)}"
    elif test_run.get("outcome") == evidence.OUTCOME_NOT_RUN:
        text += "\nThere is no recorded run to rely on: run the tests yourself and say so."
    return text
