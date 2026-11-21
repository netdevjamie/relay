"""Runs recorded before a change to evidence.py read back the same after it.

The payloads in fixtures/recorded_runs/ledger_payloads.json are real
`test_run` and `test_command_accepted` events, copied byte for byte from a
copy of the ledger. golden_outputs.json holds what Relay made of them
before the run result became a dataclass: the read API's runs and gate,
the one-line summary, whether the command counts as confirmed, and the
printed approval gate. Each test here puts the payloads into a test ledger
as they were stored and compares what Relay makes of them now with that
file, exactly.
"""

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import db
import evidence
import read_api
import recorded_runs
import routing
import run_state
import settings
from tests.test_send_back import UP_TO_REVIEW, config, finish_step, task_at  # noqa: F401  (config is a fixture)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "recorded_runs"
PAYLOADS = json.loads((FIXTURES / "ledger_payloads.json").read_text())
GOLDEN_PATH = FIXTURES / "golden_outputs.json"
UP_TO_BUILD = UP_TO_REVIEW[:-1]
# Every recorded run goes in at the Builder's step 3, as `relay next`
# would have recorded it there.
STEP = 3
RECORDED_AT = "2026-11-20T12:00:00Z"
TASKS_PLACEHOLDER = "<TASKS_DIR>"


def _insert_raw(task_id: str, event_type: str, payload: str) -> None:
    """Store the payload's text as it was in the ledger, not re-encoded."""
    connection = settings.get_db_connection()
    with connection:
        connection.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) "
            "VALUES (?,?,?,?,?,?)", (task_id, event_type, "builder", STEP, payload, RECORDED_AT))


def _printed_gate(task_id: str, config: dict) -> str:
    """What `relay next` prints at an owner gate for this task's latest step,
    or "" when the decision does not stop for the owner."""
    state = run_state.load_run_state(task_id, config)
    latest = read_api.read_latest_step(task_id, state, config)
    test_run = recorded_runs.latest_test_run(task_id, config, STEP)
    decision = routing.decide_routing(
        state, latest["phase"], latest["verdict"], config, test_run=test_run,
        test_command_confirmed=recorded_runs.is_test_command_confirmed(task_id, config, test_run))
    if decision.outcome not in (routing.ROUTE_OWNER_GATE, routing.ROUTE_RETRY_LIMIT):
        return ""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        commands._print_owner_gate(task_id, state, decision, latest["phase"], latest["verdict"],
                                   Path(latest["artifact_path"]), test_run, config)
    return printed.getvalue()


def _outputs_for(task_id: str, config: dict, runs: list[str], accepted: str | None) -> dict:
    task_at(task_id, config, UP_TO_BUILD)
    for payload in runs:
        _insert_raw(task_id, db.EVENT_TEST_RUN, payload)
    if accepted is not None:
        _insert_raw(task_id, db.EVENT_TEST_COMMAND_ACCEPTED, accepted)
    finish_step(task_id, config, "ready-for-review")
    latest = recorded_runs.latest_test_run(task_id, config, STEP)
    status = read_api.status_payload(task_id, config)
    outputs = {
        "read_test_runs": json.dumps(recorded_runs.read_test_runs(task_id, config)),
        "status_test_runs": json.dumps(status["test_runs"]),
        "gate": json.dumps(read_api.gate_check_payload(task_id, config)["gate"]),
        "summaries": [evidence.summary(run) for run in recorded_runs.read_test_runs(task_id, config)],
        "test_command_confirmed": recorded_runs.is_test_command_confirmed(task_id, config, latest),
        "printed_gate": _printed_gate(task_id, config),
    }
    return json.loads(json.dumps(outputs).replace(str(settings.TASKS_DIR), TASKS_PLACEHOLDER))


def _payload(name: str) -> str:
    return next(case["payload"] for case in PAYLOADS["test_runs"] if case["name"] == name)


# Each case is one task: its name, the recorded runs it holds (oldest
# first) and the acceptance it holds, if any.
CASES = {
    **{case["name"]: ([case["payload"]], None) for case in PAYLOADS["test_runs"]},
    "changed-command-accepted": ([_payload("changed-command")], PAYLOADS["accepted"]),
    "every-run-in-one-task": ([case["payload"] for case in PAYLOADS["test_runs"]], None),
}


def case_outputs(name: str, config: dict) -> dict:
    runs, accepted = CASES[name]
    return _outputs_for(f"golden-{name}", config, runs, accepted)


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_recorded_run_reads_back_exactly_as_before(config, case):
    assert case_outputs(case, config) == _golden()[case]


def test_the_golden_file_covers_every_recorded_payload():
    assert set(_golden()) == set(CASES)
    assert {case["name"] for case in PAYLOADS["test_runs"]} < set(CASES)


def test_the_payloads_are_stored_as_the_ledger_held_them(config):
    """The ledger's text goes in unchanged: these are not re-encoded."""
    _outputs_for("golden-raw", config, [PAYLOADS["test_runs"][0]["payload"]], None)
    stored = settings.get_db_connection().execute(
        "SELECT payload FROM events WHERE task_id = ? AND event_type = ?",
        ("golden-raw", db.EVENT_TEST_RUN)).fetchone()[0]
    assert stored == PAYLOADS["test_runs"][0]["payload"]
