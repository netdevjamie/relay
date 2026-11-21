"""Each thing that happens is recorded by its own call, when it happens.

A save writes only the task's own fields: it never works out steps,
verdicts or automatic rounds by comparing a state dict with the ledger.
A command's writes commit together, run.yaml is only a copy of the ledger,
`state_saved` is written only on a real change, and every recording call
checks phases, verdicts and statuses before it writes.

Every test builds its own ledger in a temp folder.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents
import commands
import db
import run_state
import settings
from settings import VALID_PHASES, VALID_STATUSES, VALID_VERDICTS
from tests.arbiter_fixtures import finish_arbiter
from tests.test_send_back import (  # noqa: F401  (config is a fixture)
    UP_TO_REVIEW, config, finish_step, task_at, the_arbiter_escalates,
)


@pytest.fixture
def conn(tmp_path):
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    connection = db.connect(tmp_path / "relay.db")
    yield connection
    connection.close()


def steps_rows(connection, task_id: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(
        "SELECT step, phase, verdict, artifact_filename FROM steps WHERE task_id = ? ORDER BY step",
        (task_id,))]


def event_types(connection, task_id: str) -> list[str]:
    return [event["event_type"] for event in db.get_events(connection, task_id)]


def whole_ledger(connection) -> list[list[tuple]]:
    """Every row of every table, to show a refused write changed nothing."""
    return [[tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "steps", "events")]


def save(connection, task_id: str, **changes) -> None:
    """update_task with the task's current fields, changed by `changes`."""
    task = db.get_task(connection, task_id)
    fields = {key: task[key] for key in ("status", "current_phase", "next_step", "deploy_intent",
                                         "repo", "worktree_path", "branch", "base")}
    fields.update(changes)
    db.update_task(connection, task_id, updated=None, **fields)


# ---------------------------------------------------------------------------
# No inferring from the dict
# ---------------------------------------------------------------------------

def test_a_save_ignores_history_retries_and_verdicts_in_the_dict(config):
    task_at("dict", config, UP_TO_REVIEW)
    connection = settings.get_db_connection()
    steps_before = steps_rows(connection, "dict")
    retries_before = event_types(connection, "dict").count(db.EVENT_RETRY)

    state = run_state.load_run_state("dict", config)
    state["history"].append({"phase": "builder", "step": 99, "timestamp": "2026-11-20T00:00:00Z",
                             "verdict": "ready-for-review"})
    state["retries"]["builder"] += 2
    state["history"][0]["verdict"] = "needs-human-input"
    run_state.save_run_state("dict", state, config)

    assert steps_rows(connection, "dict") == steps_before
    assert event_types(connection, "dict").count(db.EVENT_RETRY) == retries_before
    assert db.get_retry_counts(connection, "dict")["builder"] == 0


# ---------------------------------------------------------------------------
# A verdict goes on the step it was read from, never on an older one
# ---------------------------------------------------------------------------

def test_relay_next_again_at_a_gate_leaves_an_unreported_step_without_a_verdict(config):
    """Designer step 1 never reports; an override starts step 2, which stops
    at a gate. Running `relay next` again there puts the verdict on step 2
    only: step 1 never wrote a report, so it keeps no verdict."""
    task_at("again", config, [])
    assert commands.cmd_override("again", "designer", config) == 0
    finish_step("again", config, "needs-human-input")
    assert commands.cmd_next("again", auto=False, config=config) == 0
    assert commands.cmd_next("again", auto=False, config=config) == 0
    assert steps_rows(settings.get_db_connection(), "again") == [
        (1, "designer", None, "01-designer.md"),
        (2, "designer", "needs-human-input", "02-designer.md"),
    ]


def test_a_send_back_leaves_an_unreported_step_of_the_gates_phase_without_a_verdict(config):
    """Arbiter step 5 never reports; an override starts step 6, which
    escalates. Sending it back records the verdict on step 6 only."""
    task_at("sent", config, UP_TO_REVIEW)
    finish_step("sent", config, "approved_with_warnings")
    assert commands.cmd_next("sent", auto=False, config=config) == 0
    assert commands.cmd_override("sent", "arbiter", config) == 0
    finish_arbiter("sent", config, "escalate")
    assert commands.cmd_next("sent", auto=False, config=config) == 0
    assert commands.cmd_send_back("sent", config, note="Fix the edge case.") == 0
    arbiter_rows = [row for row in steps_rows(settings.get_db_connection(), "sent")
                    if row[1] == "arbiter"]
    assert arbiter_rows == [(5, "arbiter", None, "05-arbiter.md"),
                            (6, "arbiter", "escalate", "06-arbiter.md")]


# ---------------------------------------------------------------------------
# On the files backend the step is the report's file name, not its `step:`
# ---------------------------------------------------------------------------

def finish_files_step(task_id: str, config: dict, verdict: str, *, claimed_step: int,
                      extra_frontmatter: str = "") -> str:
    """Play the agent on the files backend: write the report Relay named
    for the current step, with `claimed_step` in its own `step:` line.
    Returns the report's file name."""
    state = run_state.load_run_state(task_id, config)
    latest = state["history"][-1]
    name = agents.artifact_name(latest["phase"], latest["step"], state)
    (settings.TASKS_DIR / task_id / name).write_text(
        f"---\ntask_id: {task_id}\nphase: {latest['phase']}\nstep: {claimed_step}\n"
        f"verdict: {verdict}\n{extra_frontmatter}---\n\nBody.\n")
    return name


def history_verdicts(task_id: str, config: dict) -> list[tuple]:
    return [(entry["step"], entry["phase"], entry.get("verdict"))
            for entry in run_state.load_run_state(task_id, config)["history"]]


@pytest.fixture
def files_config(config):
    return dict(config, persistence_backend="files")


def start_on_files(task_id: str, files_config: dict) -> None:
    task_dir = settings.TASKS_DIR / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text("# Brief\n")
    assert commands.cmd_start(task_id, auto=False, config=files_config) == 0


def test_on_files_a_report_claiming_a_step_that_does_not_exist_keeps_its_verdict(files_config,
                                                                                  capsys):
    """The Designer's 01-designer.md says `step: 9`. Its verdict goes on
    step 1, the number in its file name, and Relay says the two differ."""
    start_on_files("nine", files_config)
    finish_files_step("nine", files_config, "ready-for-architect", claimed_step=9)
    capsys.readouterr()
    assert commands.cmd_next("nine", auto=False, config=files_config) == 0
    assert history_verdicts("nine", files_config)[0] == (1, "designer", "ready-for-architect")
    out = capsys.readouterr().out
    assert ("Warning: 01-designer.md says it is step 9, but its file name makes it step 1. "
            "Its verdict is recorded on step 1.") in out


def test_on_files_a_fix_round_report_copying_an_older_step_keeps_its_verdict(files_config, capsys):
    """The fix-round Builder's report copies `step: 3` from the first
    build. Its verdict goes on its own step, 5, and Relay says so."""
    start_on_files("copy", files_config)
    for verdict, extra in UP_TO_REVIEW:
        finish_files_step("copy", files_config, verdict,
                          claimed_step=len(history_verdicts("copy", files_config)),
                          extra_frontmatter=extra)
        assert commands.cmd_next("copy", auto=False, config=files_config) == 0
    finish_files_step("copy", files_config, "changes_requested", claimed_step=4)
    assert commands.cmd_next("copy", auto=False, config=files_config) == 0
    fix_report = finish_files_step("copy", files_config, "ready-for-review", claimed_step=3)
    capsys.readouterr()
    assert commands.cmd_next("copy", auto=False, config=files_config) == 0
    builder_steps = [entry for entry in history_verdicts("copy", files_config)
                     if entry[1] == "builder"]
    assert builder_steps == [(3, "builder", "ready-for-review"), (5, "builder", "ready-for-review")]
    out = capsys.readouterr().out
    assert (f"Warning: {fix_report} says it is step 3, but its file name makes it step 5. "
            "Its verdict is recorded on step 5.") in out


def test_on_files_a_report_whose_step_matches_its_name_prints_no_warning(files_config, capsys):
    start_on_files("same", files_config)
    finish_files_step("same", files_config, "ready-for-architect", claimed_step=1)
    capsys.readouterr()
    assert commands.cmd_next("same", auto=False, config=files_config) == 0
    assert history_verdicts("same", files_config)[0] == (1, "designer", "ready-for-architect")
    assert "Warning:" not in capsys.readouterr().out


def test_on_files_a_step_line_with_terminal_codes_is_not_printed_raw(files_config, capsys):
    """A report's `step:` holds an erase-line code, a carriage return and
    a newline. The warning quoting it comes out on one line with none of
    them, so the report cannot wipe the warning and print its own line."""
    start_on_files("codes", files_config)
    (settings.TASKS_DIR / "codes" / "01-designer.md").write_text(
        '---\ntask_id: codes\nphase: designer\n'
        'step: "1\\e[2K\\rRouting: all checks passed\\nX"\n'
        'verdict: ready-for-architect\n---\n\nBody.\n')
    capsys.readouterr()
    assert commands.cmd_next("codes", auto=False, config=files_config) == 0
    assert history_verdicts("codes", files_config)[0] == (1, "designer", "ready-for-architect")
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out
    warning = next(line for line in out.splitlines() if "Warning:" in line)
    assert warning == ("  Warning: 01-designer.md says it is step 1[2K Routing: all checks passed X, "
                       "but its file name makes it step 1. Its verdict is recorded on step 1.")


# ---------------------------------------------------------------------------
# Each explicit call records its one thing, and nothing before it
# ---------------------------------------------------------------------------

def test_record_dispatch_adds_the_step_row_with_its_filename(conn, tmp_path):
    db.create_task(conn, "t")
    assert steps_rows(conn, "t") == []
    db.record_dispatch(conn, "t", phase="reviewer", step=5, artifact_filename="05-reviewer-pass-2.md",
                       created_at="2026-11-20T00:00:00Z", task_dir=tmp_path)
    assert steps_rows(conn, "t") == [(5, "reviewer", None, "05-reviewer-pass-2.md")]
    assert db.get_history(conn, "t")[0]["timestamp"] == "2026-11-20T00:00:00Z"


def test_record_verdict_sets_the_steps_verdict(conn):
    db.create_task(conn, "t")
    db.record_dispatch(conn, "t", phase="reviewer", step=1, artifact_filename="01-reviewer.md",
                       created_at="2026-11-20T00:00:00Z")
    assert steps_rows(conn, "t")[0][2] is None
    db.record_verdict(conn, "t", step=1, phase="reviewer", verdict="approved")
    assert steps_rows(conn, "t")[0][2] == "approved"


def test_record_verdict_refuses_a_step_that_is_not_there_or_of_another_phase(conn):
    db.create_task(conn, "t")
    db.record_dispatch(conn, "t", phase="builder", step=1, artifact_filename="01-builder.md",
                       created_at="2026-11-20T00:00:00Z")
    before = whole_ledger(conn)
    with pytest.raises(ValueError):
        db.record_verdict(conn, "t", step=2, phase="builder", verdict="ready-for-review")
    with pytest.raises(ValueError):
        db.record_verdict(conn, "t", step=1, phase="reviewer", verdict="approved")
    assert whole_ledger(conn) == before


def test_record_retry_adds_one_retry_event(conn):
    db.create_task(conn, "t")
    assert db.EVENT_RETRY not in event_types(conn, "t")
    db.record_retry(conn, "t", phase="builder")
    assert event_types(conn, "t").count(db.EVENT_RETRY) == 1
    assert db.get_retry_counts(conn, "t")["builder"] == 1


def test_update_task_writes_state_saved_on_a_status_change(conn):
    db.create_task(conn, "t")
    assert db.EVENT_STATE_SAVED not in event_types(conn, "t")
    save(conn, "t", status="blocked")
    (saved,) = [event for event in db.get_events(conn, "t")
                if event["event_type"] == db.EVENT_STATE_SAVED]
    assert saved["payload"] == {"status": ["in_progress", "blocked"],
                                "current_phase": ["architect", "architect"]}
    assert db.get_task(conn, "t")["status"] == "blocked"


def test_update_task_writes_state_saved_on_a_phase_change(conn):
    db.create_task(conn, "t")
    assert db.EVENT_STATE_SAVED not in event_types(conn, "t")
    save(conn, "t", current_phase="builder")
    (saved,) = [event for event in db.get_events(conn, "t")
                if event["event_type"] == db.EVENT_STATE_SAVED]
    assert saved["payload"] == {"status": ["in_progress", "in_progress"],
                                "current_phase": ["architect", "builder"]}
    assert saved["phase"] == "builder"


def test_update_task_writes_no_state_saved_when_nothing_changed(conn):
    db.create_task(conn, "t")
    save(conn, "t", deploy_intent=True, worktree_path="/elsewhere", next_step=4)
    assert event_types(conn, "t") == [db.EVENT_TASK_CREATED]
    task = db.get_task(conn, "t")
    assert (task["deploy_intent"], task["worktree_path"], task["next_step"]) == (True, "/elsewhere", 4)


def test_update_task_refuses_a_task_that_is_not_there(conn):
    fields = dict(status="in_progress", current_phase="builder", next_step=1, updated=None,
                  deploy_intent=False, repo=None, worktree_path=None, branch=None, base=None)
    with pytest.raises(ValueError):
        db.update_task(conn, "missing", **fields)
    assert whole_ledger(conn) == [[], [], []]


def test_old_state_saved_rows_still_load(conn):
    """A ledger from before this change holds a state_saved for every save,
    most of them changing nothing. They stay, and still read."""
    db.create_task(conn, "old")
    no_change = json.dumps({"status": ["in_progress", "in_progress"],
                            "current_phase": ["architect", "architect"]})
    with db.transaction(conn):
        for _ in range(3):
            conn.execute("INSERT INTO events (task_id, event_type, phase, payload, timestamp) "
                         "VALUES (?,?,?,?,?)",
                         ("old", db.EVENT_STATE_SAVED, "architect", no_change, "2026-10-01T00:00:00Z"))
    assert db.get_task(conn, "old")["current_phase"] == "architect"
    assert event_types(conn, "old") == [db.EVENT_TASK_CREATED] + [db.EVENT_STATE_SAVED] * 3


# ---------------------------------------------------------------------------
# Every recording call checks the enums before it writes
# ---------------------------------------------------------------------------

def _create_unknown_phase(connection):
    db.create_task(connection, "new", current_phase="not-a-phase")


def _update_unknown_phase(connection):
    save(connection, "t", current_phase="not-a-phase")


def _update_unknown_status(connection):
    save(connection, "t", status="not-a-status")


def _dispatch_unknown_phase(connection):
    db.record_dispatch(connection, "t", phase="not-a-phase", step=2,
                       artifact_filename="02-x.md", created_at="2026-11-20T00:00:00Z")


def _verdict_unknown_phase(connection):
    db.record_verdict(connection, "t", step=1, phase="not-a-phase", verdict="approved")


def _verdict_unknown_verdict(connection):
    db.record_verdict(connection, "t", step=1, phase="architect", verdict="not-a-verdict")


def _retry_unknown_phase(connection):
    db.record_retry(connection, "t", phase="not-a-phase")


@pytest.mark.parametrize("call", [
    _create_unknown_phase, _update_unknown_phase, _update_unknown_status, _dispatch_unknown_phase,
    _verdict_unknown_phase, _verdict_unknown_verdict, _retry_unknown_phase,
], ids=lambda call: call.__name__.lstrip("_"))
def test_each_explicit_call_rejects_unknown_values(conn, call):
    db.create_task(conn, "t")
    db.record_dispatch(conn, "t", phase="architect", step=1, artifact_filename="01-architect.md",
                       created_at="2026-11-20T00:00:00Z")
    before = whole_ledger(conn)
    with pytest.raises(db.InvalidTransition):
        call(conn)
    assert whole_ledger(conn) == before


# ---------------------------------------------------------------------------
# run.yaml is only a copy of the ledger
# ---------------------------------------------------------------------------

def _hand_edit(task_id: str) -> None:
    """Change what run.yaml says the task is, the way a person editing the
    file would: its phase, its status, a step it never had, a retry count."""
    path = settings.TASKS_DIR / task_id / "run.yaml"
    text = path.read_text()
    edited = (text.replace("current_phase: arbiter", "current_phase: ops")
                  .replace("status: in_progress", "status: completed")
                  .replace("  builder: 0", "  builder: 7")
                  + "- phase: ops\n  step: 40\n  timestamp: '2026-01-01T00:00:00Z'\n")
    assert edited.count("ops") >= 2 and "builder: 7" in edited
    path.write_text(edited)


def _ledger_view(task_id: str) -> str:
    """What the ledger holds for the task, without times or its name."""
    connection = settings.get_db_connection()
    task = db.get_task(connection, task_id)
    events = [{key: event.get(key) for key in ("event_type", "phase", "step", "verdict", "payload")}
              for event in db.get_events(connection, task_id)]
    for entry in task["history"]:
        entry.pop("timestamp")
    for key in ("task_id", "created", "updated"):
        task.pop(key)
    return json.dumps([task, events, steps_rows(connection, task_id)]).replace(task_id, "<ID>")


def test_a_hand_edited_run_yaml_changes_nothing(config, capsys):
    for task_id in ("edited", "twin"):
        task_at(task_id, config, UP_TO_REVIEW)
        the_arbiter_escalates(task_id, config)
    _hand_edit("edited")

    for task_id in ("edited", "twin"):
        commands.cmd_status(task_id, config)
        assert commands.cmd_send_back(task_id, config, note="Fix the warnings.") == 0
        finish_step(task_id, config, "ready-for-review")
        assert commands.cmd_next(task_id, auto=False, config=config) == 0
    assert "completed" not in capsys.readouterr().out

    assert _ledger_view("edited") == _ledger_view("twin")
    run_yaml = (settings.TASKS_DIR / "edited" / "run.yaml").read_text()
    assert run_yaml == db.generate_run_yaml(settings.get_db_connection(), "edited")
    assert "builder: 7" not in run_yaml and "step: 40" not in run_yaml


# ---------------------------------------------------------------------------
# A command's writes commit together
# ---------------------------------------------------------------------------

def _fail_after(monkeypatch, name: str) -> None:
    """Make db.`name` do its write and then fail, as if the process died
    right after it: everything the command wrote before it, and the write
    itself, must be rolled back."""
    real = getattr(db, name)

    def write_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise sqlite3.OperationalError(f"injected failure after {name}")
    monkeypatch.setattr(db, name, write_then_fail)


def _at_an_escalation(task_id: str, config: dict) -> None:
    task_at(task_id, config, UP_TO_REVIEW)
    the_arbiter_escalates(task_id, config)


def _at_a_plan_that_deploys(task_id: str, config: dict) -> None:
    """The Architect's report says deploy: yes, and `relay next` has not
    read it. Its verdict is synced first, as `relay next` does before
    anything it would roll back."""
    task_at(task_id, config, [("ready-for-architect", "")])
    finish_step(task_id, config, "ready-for-builder", "deploy: yes\n")
    run_state.find_latest_artifact(task_id, config)


def _brief_only(task_id: str, config: dict) -> None:
    task_dir = settings.TASKS_DIR / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text("# Brief\n")


ATOMIC_COMMANDS = {
    "send-back": (_at_an_escalation, "record_sent_back",
                  lambda task_id, config: commands.cmd_send_back(task_id, config, note="Fix it.")),
    "override with a note": (_at_an_escalation, "update_task",
                             lambda task_id, config: commands.cmd_override(
                                 task_id, "builder", config, note="Go on.")),
    "note": (_at_an_escalation, "record_owner_note",
             lambda task_id, config: commands.cmd_note(task_id, config, "Remember this.")),
    "start": (_brief_only, "record_event",
              lambda task_id, config: commands.cmd_start(task_id, auto=False, config=config)),
    "deploy flag": (_at_a_plan_that_deploys, "update_task",
                    lambda task_id, config: commands.cmd_next(task_id, auto=False, config=config)),
}


def _everything_about(task_id: str) -> tuple:
    connection = settings.get_db_connection()
    rows = tuple(tuple(tuple(row) for row in connection.execute(
        f"SELECT * FROM {table} WHERE task_id = ? ORDER BY rowid", (task_id,)))
        for table in ("tasks", "steps", "events"))
    run_yaml = settings.TASKS_DIR / task_id / "run.yaml"
    return rows, run_yaml.read_text() if run_yaml.exists() else None


@pytest.mark.parametrize("command", list(ATOMIC_COMMANDS))
def test_a_command_that_fails_part_way_leaves_nothing_behind(command, config, monkeypatch):
    prepare, failing_write, run = ATOMIC_COMMANDS[command]
    prepare("atomic", config)
    before = _everything_about("atomic")
    _fail_after(monkeypatch, failing_write)
    with pytest.raises(sqlite3.OperationalError, match="injected failure"):
        run("atomic", config)
    assert not settings.get_db_connection().in_transaction
    assert _everything_about("atomic") == before
