"""Relay's own test runs in the ledger: running the repo's tests for a
step (via evidence.py), reading recorded runs back, and whether the owner
has accepted a changed test command.
"""

from pathlib import Path

import base_trust
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
    """True when the owner has accepted everything this run flags for
    confirmation, for this task. Only acceptances recorded at the gate that
    asked count (see _accept_test_command); no other approval stands in.

    - A changed test command or report path: an acceptance of this exact
      command and report path. One covers the rest of the task; a new
      command asks again.
    - Changed harness files: an acceptance of the same harness digest. A
      further change to a harness file asks again.
    - Fewer tests run: an acceptance of this many tests run or fewer. A
      later run with fewer asks again.
    - Harness files Relay could not check: an acceptance at this commit.
      A later commit asks again."""
    if not test_run or not _sqlite_enabled(config):
        return False
    accepted = [event.get("payload") or {} for event in db.get_events(get_db_connection(), task_id)
                if event["event_type"] == db.EVENT_TEST_COMMAND_ACCEPTED]
    setup = (test_run.get("command"), test_run.get("test_report"))
    command_ok = any((payload.get("command"), payload.get("test_report")) == setup for payload in accepted)
    harness_changed, tests_drop = test_run.get("harness_changed"), test_run.get("tests_drop")
    harness_unchecked = test_run.get("harness_unchecked")
    if not harness_changed and not tests_drop and not harness_unchecked:
        return command_ok
    harness_ok = not harness_changed or any(
        payload.get("harness_digest") == test_run.get("harness_digest") for payload in accepted)
    count_ok = not tests_drop or any(_covers_count(payload.get("tests_run"), test_run.get("tests_run"))
                                     for payload in accepted)
    unchecked_ok = not harness_unchecked or any(
        payload.get("harness_unchecked_at") == test_run.get("commit") for payload in accepted)
    return ((not test_run.get("command_differs_from_base") or command_ok) and harness_ok and count_ok
            and unchecked_ok)


def _covers_count(accepted_count, tests_run) -> bool:
    """True when an accepted count is a whole number no larger than this run's."""
    numbers = [value for value in (accepted_count, tests_run)
               if isinstance(value, int) and not isinstance(value, bool)]
    return len(numbers) == 2 and accepted_count <= tests_run


def _test_command_awaits_owner(task_id: str, config: dict, phase: str, verdict: str,
                               test_run: dict | None) -> bool:
    """True when a passing run is waiting for the owner to confirm what it
    flags (evidence.needs_owner_confirmation)."""
    return bool(test_run and (phase, verdict) in VERDICTS_RELAY_TESTS
                and test_run.get("outcome") == evidence.OUTCOME_PASSED
                and evidence.needs_owner_confirmation(test_run)
                and not is_test_command_confirmed(task_id, config, test_run))


def ensure_test_run(task_id: str, state: dict, phase: str, verdict: str, step,
                    config: dict) -> dict | None:
    """Run the repo's tests for a step that just reported "ready", record
    the run in the ledger and return it. None when this verdict needs no
    run, or on the files backend (which keeps no events).

    A task with no working directory, or with a base Relay has no confirmed
    commit for (base_trust.py), gets a `not_run` record, so the gap is
    visible; the base is never read by name. A run already recorded for this step at the commit the worktree
    is on is reused, so running `relay next` twice does not test twice."""
    if (phase, verdict) not in VERDICTS_RELAY_TESTS or not _sqlite_enabled(config):
        return None
    step = step if isinstance(step, int) else None
    recorded = latest_test_run(task_id, config, step)
    work_dir = _task_cwd(state)
    base_commit = base_trust.trusted_commit(state)
    if not work_dir or not Path(work_dir).is_dir():
        if recorded and recorded.get("outcome") == evidence.OUTCOME_NOT_RUN:
            return recorded
        run = evidence.not_run("The task has no working directory, so Relay had nowhere to run tests.")
    elif base_trust.has_base(state) and base_commit is None:
        run = evidence.not_run(base_trust.NO_TRUSTED_COMMIT)
    else:
        worktree = Path(work_dir)
        conclusive = (evidence.OUTCOME_PASSED, evidence.OUTCOME_FAILED, evidence.OUTCOME_TIMED_OUT)
        if (recorded and recorded.get("outcome") in conclusive
                and recorded.get("commit") == evidence.head_commit(worktree)
                and not evidence.uncommitted_changes(worktree)):
            return recorded
        print(f"  Running the repo's tests (Relay's own run)...")
        log_name = f"{step:02d}-test-run.log" if step is not None else "test-run.log"
        run = evidence.run_tests(worktree, settings.TASKS_DIR / task_id / log_name, base_commit=base_commit,
                                 timeout_seconds=config.get("test_timeout_seconds",
                                                            evidence.DEFAULT_TIMEOUT_SECONDS),
                                 earlier_runs=read_test_runs(task_id, config))
    db.record_test_run(get_db_connection(), task_id, phase=phase, step=step, run=run.to_record())
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
