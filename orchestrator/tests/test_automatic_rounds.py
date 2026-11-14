"""The retry limit counts only rounds nobody chose.

An automatic round is Relay routing a task back to an earlier phase on its
own (a Reviewer's changes requested, say). It counts toward `max_retries`.
A round the owner chose, with `relay send-back` or `relay override`, is
never counted, is never refused by the limit, and starts the count for the
phase it goes to again.

Every test here builds its own ledger in a temp folder.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents
import board_view
import commands
import db
import read_api
import routing
import run_state
import settings
from settings import DEFAULT_CONFIG
from tests.test_send_back import (  # noqa: F401
    UP_TO_REVIEW, before_the_stop_card, config, events_of_type, finish_step, task_at,
)


@pytest.fixture(autouse=True)
def the_walks_here_were_written_for_five_fix_rounds(request, monkeypatch):
    """The walks in this file test how the tripwire and the retry limit
    work, with the tripwire set at five fix rounds so each walk has room.
    The real number is two; TestTheOwnersNumber below tests it."""
    if request.cls is not TestTheOwnersNumber:
        monkeypatch.setattr(routing, "FIX_ROUNDS_BEFORE_THE_OWNER_DECIDES", 5)

NOON = datetime(2026, 10, 7, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
CLI = settings._CLI_CMD


def next_(task_id: str, config: dict) -> None:
    commands.cmd_next(task_id, auto=False, config=config)


def reviewer_asks_for_changes(task_id: str, config: dict) -> None:
    """An automatic round: Relay routes the task back to the Builder itself
    (or stops, at the limit)."""
    finish_step(task_id, config, "changes_requested")
    next_(task_id, config)


def builder_finishes(task_id: str, config: dict) -> None:
    finish_step(task_id, config, "ready-for-review")
    next_(task_id, config)


def automatic_rounds(task_id: str, config: dict, rounds: int) -> None:
    """Start a task and run this many automatic rounds, leaving the Reviewer running."""
    task_at(task_id, config, UP_TO_REVIEW)
    for _ in range(rounds):
        reviewer_asks_for_changes(task_id, config)
        builder_finishes(task_id, config)


def send_back_at_the_gate(task_id: str, config: dict, note: str | None = "Fix the warnings.") -> None:
    """A chosen round: the Reviewer passes with warnings, Relay stops for
    the owner, and the owner sends the work back."""
    finish_step(task_id, config, "approved_with_warnings")
    next_(task_id, config)
    commands.cmd_send_back(task_id, config, note=note)


def count(task_id: str, config: dict, phase: str = "builder") -> int:
    return read_api.status_payload(task_id, config)["retries"].get(phase, 0)


def state_of(task_id: str, config: dict) -> tuple[str, str]:
    task = read_api.status_payload(task_id, config)["task"]
    return task["status"], task["current_phase"]


def steps(task_id: str, config: dict) -> list[str]:
    return [entry["phase"] for entry in read_api.status_payload(task_id, config)["history"]]


def files_backend_task_in_review(task_id: str, tmp_path, monkeypatch):
    """A task on the legacy files backend with the Reviewer running. Returns
    its config and a function that plays the agent and runs `relay next`."""
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    files_config = dict(DEFAULT_CONFIG)
    files_config["human_gates"] = {name: False for name in DEFAULT_CONFIG["human_gates"]}

    def finish(verdict: str, extra_frontmatter: str = "") -> None:
        """On this backend a step's file name is known only once the file exists."""
        state = run_state.load_run_state(task_id, files_config)
        phase, step = state["history"][-1]["phase"], state["history"][-1]["step"]
        (settings.TASKS_DIR / task_id / agents.artifact_name(phase, step, state)).write_text(
            f"---\ntask_id: {task_id}\nphase: {phase}\nstep: {step}\n"
            f"verdict: {verdict}\n{extra_frontmatter}---\n\nBody.\n")
        next_(task_id, files_config)

    task_at(task_id, files_config, [])
    for verdict, extra_frontmatter in UP_TO_REVIEW:
        finish(verdict, extra_frontmatter)
    return files_config, finish


def view_of(task_id: str, config: dict) -> dict:
    return board_view.gate_view(read_api.status_payload(task_id, config), NOON)


def waiting_rows(config: dict) -> list[tuple[str, str]]:
    """(task id, its one-line state) for every task waiting on the owner."""
    return [(row["task_id"], board_view.row_line(row, NOON))
            for row in read_api.list_payload(config)["tasks"]
            if row["gate_state"] == "waiting_on_owner"]


def refused(command, task_id: str, config: dict, capsys) -> str:
    """Run a command that must exit 1 and change nothing in the ledger; return what it printed."""
    before = db.get_events(settings.get_db_connection(), task_id)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        command(task_id, config)
    assert exit_info.value.code == 1
    assert db.get_events(settings.get_db_connection(), task_id) == before
    return capsys.readouterr().out


class TestTheCount:
    def test_an_automatic_loop_back_counts(self, config):
        task_at("auto", config, UP_TO_REVIEW)
        reviewer_asks_for_changes("auto", config)
        assert state_of("auto", config) == ("in_progress", "builder")
        assert count("auto", config) == 1
        assert [event["phase"] for event in events_of_type("auto", db.EVENT_RETRY)] == ["builder"]

    def test_a_gate_send_back_does_not_count(self, config):
        task_at("chosen", config, UP_TO_REVIEW)
        send_back_at_the_gate("chosen", config)
        assert state_of("chosen", config) == ("in_progress", "builder")
        assert count("chosen", config) == 0
        assert events_of_type("chosen", db.EVENT_RETRY) == []

    def test_a_gate_send_back_resets_the_count(self, config):
        automatic_rounds("reset", config, 1)
        assert count("reset", config) == 1
        send_back_at_the_gate("reset", config)
        assert count("reset", config) == 0
        builder_finishes("reset", config)
        reviewer_asks_for_changes("reset", config)
        assert count("reset", config) == 1  # the next automatic round is the first again

    def test_override_resets_the_count_of_the_phase_it_moves_to(self, config):
        automatic_rounds("moved", config, 2)
        finish_step("moved", config, "approved")
        commands.cmd_override("moved", "tester", config)
        assert count("moved", config) == 2  # an override elsewhere leaves the Builder's count
        finish_step("moved", config, "pass")
        commands.cmd_override("moved", "builder", config)
        assert count("moved", config) == 0
        # The reset is the approval event itself: no retry event was added or needed.
        assert len(events_of_type("moved", db.EVENT_RETRY)) == 2
        approvals = [event["payload"] for event in events_of_type("moved", db.EVENT_APPROVAL)]
        assert approvals == [{"next_phase": "tester", "method": "override", "note": None},
                             {"next_phase": "builder", "method": "override", "note": None}]

    def test_override_out_of_a_limit_stop_resets_the_stopped_phase_wherever_it_goes(self, config):
        """The stop offers `override <phase>`. Taking it is the owner's
        decision, so the next automatic round into the stopped phase is the first."""
        automatic_rounds("decided", config, 3)
        reviewer_asks_for_changes("decided", config)  # stopped: builder 3 of 3
        assert (state_of("decided", config), count("decided", config)) == (("blocked", "reviewer"), 3)

        commands.cmd_override("decided", "tester", config)
        assert (state_of("decided", config), count("decided", config)) == (("in_progress", "tester"), 0)
        # The reset is the approval event: it names the stopped phase, and no retry event changed.
        assert [event["payload"] for event in events_of_type("decided", db.EVENT_APPROVAL)] == [
            {"next_phase": "tester", "method": "override", "note": None, "stopped_phase": "builder"}]
        assert len(events_of_type("decided", db.EVENT_RETRY)) == 3

        finish_step("decided", config, "fail")
        next_("decided", config)  # an automatic round into the Builder, and it runs
        assert (state_of("decided", config), count("decided", config)) == (("in_progress", "builder"), 1)

    def test_files_backend_override_out_of_a_limit_stop_resets_the_stopped_phase(
            self, tmp_path, monkeypatch):
        files_config, finish = files_backend_task_in_review("moved-on", tmp_path, monkeypatch)
        files_config["max_retries"] = 0
        finish("changes_requested")  # stopped: the limit allows no automatic round
        state = run_state.load_run_state("moved-on", files_config)
        state["retries"]["builder"] = 2  # an old stored count
        run_state.save_run_state("moved-on", state, files_config)
        assert state_of("moved-on", files_config) == ("blocked", "reviewer")

        commands.cmd_override("moved-on", "tester", files_config)
        assert (state_of("moved-on", files_config), count("moved-on", files_config)) == (
            ("in_progress", "tester"), 0)

    @pytest.mark.parametrize("event_type, payload", [
        (db.EVENT_SENT_BACK, {"to_phase": ["builder"]}),
        (db.EVENT_SENT_BACK, {"to_phase": {"builder": 1}}),
        (db.EVENT_SENT_BACK, {"to_phase": 7}),
        (db.EVENT_APPROVAL, {"method": "override", "next_phase": ["builder"]}),
        (db.EVENT_APPROVAL, {"method": "override", "next_phase": {"builder": 1}}),
        (db.EVENT_APPROVAL, {"method": "override", "next_phase": "tester", "stopped_phase": ["builder"]}),
        (db.EVENT_APPROVAL, {"method": "override", "next_phase": "tester", "stopped_phase": {"builder": 1}}),
        (db.EVENT_APPROVAL, {"method": "override", "next_phase": "tester", "stopped_phase": 3}),
        (db.EVENT_APPROVAL, {"method": "override", "next_phase": "tester", "stopped_phase": True}),
        (db.EVENT_APPROVAL, {"method": "override", "next_phase": "tester", "stopped_phase": None}),
    ])
    def test_a_decision_whose_phase_is_not_text_resets_nothing_and_raises_nothing(
            self, config, event_type, payload):
        """Only a hand-edited ledger can hold such a row. It must not break
        status, the list or the card for that ledger."""
        automatic_rounds("odd", config, 2)
        with settings.get_db_connection() as conn:
            conn.execute(
                "INSERT INTO events (task_id, event_type, phase, verdict, payload, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                ("odd", event_type, "reviewer", None, json.dumps(payload), "2026-10-07T12:00:00Z"))
        assert count("odd", config) == 2
        assert len(read_api.list_payload(config)["tasks"]) == 1

    def test_a_payload_nested_too_deep_to_read_resets_nothing_and_raises_nothing(self, config):
        """Reading it raises RecursionError, which is not a ValueError. One
        such row must not take the list and the card down for every task."""
        automatic_rounds("deep", config, 2)
        automatic_rounds("other", config, 1)
        with settings.get_db_connection() as conn:
            conn.execute(
                "INSERT INTO events (task_id, event_type, phase, verdict, payload, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                ("deep", db.EVENT_SENT_BACK, "reviewer", None, "[" * 100000 + "]" * 100000,
                 "2026-10-07T12:00:00Z"))
            assert db.get_retry_counts(conn, "deep")["builder"] == 2
        assert len(read_api.list_payload(config)["tasks"]) == 2

    def test_replay_one_automatic_round_then_four_gate_send_backs_never_reaches_limit_3(self, config):
        """The history of the task this rule came from, with one send-back more."""
        automatic_rounds("replay", config, 1)
        counts = [count("replay", config)]
        for round_number in range(4):
            send_back_at_the_gate("replay", config, note=f"Finding {round_number + 1}.")
            assert state_of("replay", config) == ("in_progress", "builder")
            counts.append(count("replay", config))
            builder_finishes("replay", config)
        assert counts == [1, 0, 0, 0, 0]
        assert events_of_type("replay", db.EVENT_APPROVAL) == []  # no override was needed


class TestTheLimit:
    def test_the_limit_stops_the_fourth_automatic_round_in_a_row(self, config, capsys):
        task_at("four", config, UP_TO_REVIEW)
        for round_number in (1, 2, 3):
            reviewer_asks_for_changes("four", config)
            assert (state_of("four", config), count("four", config)) == (
                ("in_progress", "builder"), round_number)
            builder_finishes("four", config)
        steps_before = steps("four", config)

        capsys.readouterr()
        reviewer_asks_for_changes("four", config)
        out = before_the_stop_card(capsys.readouterr().out, "four", config)
        assert state_of("four", config) == ("blocked", "reviewer")
        assert steps("four", config) == steps_before  # no fourth round was started
        assert out.endswith(
            "\n  STOPPED: builder has gone back for fixes 3 times in a row with nobody deciding (limit 3).\n"
            "  Read the latest report, then choose:\n"
            f"    {CLI} send-back four --note \"what to fix\"   (one more round; the count starts again)\n"
            f"    {CLI} override four <phase>                 (move it somewhere else)\n"
            f"    {CLI} abort four\n")

        out = before_the_stop_card(refused(next_, "four", config, capsys), "four", config)
        assert out == (
            "Task four is stopped: builder has gone back for fixes 3 times in a row "
            "with nobody deciding (limit 3).\n"
            "  Read the latest report, then choose:\n"
            f"    {CLI} send-back four --note \"what to fix\"   (one more round; the count starts again)\n"
            f"    {CLI} override four <phase>                 (move it somewhere else)\n"
            f"    {CLI} abort four\n")

    def test_send_back_is_never_refused_by_the_limit(self, config):
        config["max_retries"] = 1
        automatic_rounds("never", config, 1)  # the count is at the limit already
        for _ in range(3):
            send_back_at_the_gate("never", config)
            assert state_of("never", config) == ("in_progress", "builder")
            builder_finishes("never", config)
        assert steps("never", config).count("builder") == 5
        assert events_of_type("never", db.EVENT_APPROVAL) == []

    @pytest.mark.parametrize("next_has_stopped_it", [False, True])
    def test_send_back_from_the_limit_stop_runs_one_more_round(self, config, capsys,
                                                               next_has_stopped_it):
        config["max_retries"] = 1
        automatic_rounds("stopped", config, 1)
        finish_step("stopped", config, "changes_requested")
        if next_has_stopped_it:
            next_("stopped", config)
            assert state_of("stopped", config) == ("blocked", "reviewer")

        capsys.readouterr()
        commands.cmd_send_back("stopped", config, note="One more go at the null check.")
        out = capsys.readouterr().out
        assert "Sent back to builder. Automatic rounds: 0 of 1." in out
        assert "One more go at the null check." in out  # the Builder is given the note
        assert state_of("stopped", config) == ("in_progress", "builder")
        assert count("stopped", config) == 0
        assert steps("stopped", config)[-2:] == ["reviewer", "builder"]

        builder_finishes("stopped", config)
        reviewer_asks_for_changes("stopped", config)  # the next automatic round runs
        assert (state_of("stopped", config), count("stopped", config)) == (("in_progress", "builder"), 1)

    def test_the_stop_goes_back_to_the_phase_that_was_stopped(self, config):
        """An Architect's `needs-designer` loop is stopped into the Designer,
        so that is where a send-back from the stop goes."""
        config["max_retries"] = 0
        task_at("brief-loop", config, [("ready-for-architect", "")])
        finish_step("brief-loop", config, "needs-designer")
        next_("brief-loop", config)
        assert state_of("brief-loop", config) == ("blocked", "architect")
        commands.cmd_send_back("brief-loop", config, note="Drop the second screen.")
        assert state_of("brief-loop", config) == ("in_progress", "designer")
        assert events_of_type("brief-loop", db.EVENT_SENT_BACK)[0]["payload"]["to_phase"] == "designer"


class TestTheLedger:
    def test_every_send_back_is_a_sent_back_event_and_never_an_approval(self, config):
        config["max_retries"] = 1
        task_at("label", config, UP_TO_REVIEW)
        send_back_at_the_gate("label", config, note="At a gate, with a note.")
        builder_finishes("label", config)
        reviewer_asks_for_changes("label", config)   # automatic: the count is at the limit
        builder_finishes("label", config)
        reviewer_asks_for_changes("label", config)   # stopped by the limit
        commands.cmd_send_back("label", config, note="From the limit stop.")
        builder_finishes("label", config)
        send_back_at_the_gate("label", config, note=None)

        sent_back = [(event["phase"], event["verdict"], event["payload"])
                     for event in events_of_type("label", db.EVENT_SENT_BACK)]
        assert sent_back == [
            ("reviewer", "approved_with_warnings",
             {"to_phase": "builder", "note": "At a gate, with a note."}),
            ("reviewer", "changes_requested",
             {"to_phase": "builder", "note": "From the limit stop."}),
            ("reviewer", "approved_with_warnings", {"to_phase": "builder", "note": None}),
        ]
        assert events_of_type("label", db.EVENT_APPROVAL) == []
        assert read_api.status_payload("label", config)["approvals"] == []

    def test_a_send_back_with_no_note_resets_and_says_so(self, config, capsys):
        automatic_rounds("quiet", config, 1)
        finish_step("quiet", config, "approved_with_warnings")
        next_("quiet", config)
        capsys.readouterr()
        commands.cmd_send_back("quiet", config)
        out = capsys.readouterr().out
        assert "Sent back to builder. Automatic rounds: 0 of 3.\n" in out
        assert f"No note recorded. Add one with: {CLI} note quiet --note \"...\"\n" in out
        assert count("quiet", config) == 0

        builder_finishes("quiet", config)
        finish_step("quiet", config, "approved_with_warnings")
        next_("quiet", config)
        capsys.readouterr()
        commands.cmd_send_back("quiet", config, note="Rename the helper.")
        assert "No note recorded" not in capsys.readouterr().out

    def test_new_code_writes_no_event_the_installed_version_cannot_read(self, config):
        """A rollback puts the installed version back over this ledger. It
        must find only event types and payload keys it already knows, and
        its own count (every retry event, whatever followed) must not be
        above the number of automatic rounds that really ran."""
        installed_event_types = {
            "task_created", "state_saved", "retry", "approval", "worktree_created",
            "worktree_finalized", "migration_override", "historical_note",
            "step_never_delivered", "deploy_decided", "sent_back", "test_run",
            "test_command_accepted", "slept_during_step", "owner_note", "verdict_recorded",
            # Written at start. The installed version reads events
            # only by the types it asks for, so it passes over this one.
            "sized"}
        config["max_retries"] = 1
        automatic_rounds("rollback", config, 1)
        send_back_at_the_gate("rollback", config)
        builder_finishes("rollback", config)
        reviewer_asks_for_changes("rollback", config)
        builder_finishes("rollback", config)
        reviewer_asks_for_changes("rollback", config)            # stopped by the limit
        commands.cmd_send_back("rollback", config, note="Once more.")
        builder_finishes("rollback", config)
        finish_step("rollback", config, "approved")
        commands.cmd_override("rollback", "builder", config, note="And the README.")

        events = db.get_events(settings.get_db_connection(), "rollback")
        assert {event["event_type"] for event in events} <= installed_event_types
        for event in events:
            if event["event_type"] == db.EVENT_SENT_BACK:
                # The installed version reads to_phase and note; it never needs the key it used to add.
                assert set(event["payload"]) == {"to_phase", "note"}
            if event["event_type"] == db.EVENT_APPROVAL:
                assert set(event["payload"]) == {"next_phase", "method", "note"}
            if event["event_type"] == db.EVENT_RETRY:
                assert "payload" not in event
        assert len(events_of_type("rollback", db.EVENT_RETRY)) == 2  # the two automatic rounds
        assert state_of("rollback", config)[0] == "in_progress"

    def test_the_report_after_a_send_back_is_still_named_fix_n(self, config):
        """The count goes back to zero; the file name does not."""
        automatic_rounds("named", config, 1)
        send_back_at_the_gate("named", config)
        builder_finishes("named", config)
        finish_step("named", config, "approved")
        commands.cmd_override("named", "builder", config)
        names = [entry["artifact"] for entry in read_api.status_payload("named", config)["history"]
                 if entry["phase"] == "builder"]
        assert names == ["03-builder.md", "05-builder-fix-1.md", "07-builder-fix-2.md",
                         "09-builder-fix-3.md"]


def installed_version_send_back(task_id: str, config: dict, *, refused_at_the_limit: bool = False) -> None:
    """Write what `relay send-back` wrote before this rule, in the order it
    wrote it (read from origin/main at 1affd89 and checked by running that
    code on a scratch ledger).

    Accepted: one save holding `state_saved`, the new Builder step and a
    `retry` event, then a `sent_back` event whose payload also carries
    `stopped_at_retry_limit: false`. Refused at the limit: one save that
    sets the task `blocked`, then a `sent_back` event marked `true`; no
    step and no `retry` event."""
    conn = settings.get_db_connection()
    state = run_state.load_run_state(task_id, config)
    latest = read_api.read_latest_step(task_id, state, config)
    run_state._record_verdict_in_history(state, latest["phase"], latest["verdict"])
    if refused_at_the_limit:
        state["status"] = "blocked"
    else:
        run_state.increment_retry(state, "builder")
        step = run_state.get_next_step(task_id, config)
        state["current_phase"], state["next_step"] = "builder", step + 1
        run_state.record_phase(state, "builder", step)
    run_state.save_run_state(task_id, state, config)
    payload = {"to_phase": "builder", "note": "An old note.",
               "stopped_at_retry_limit": refused_at_the_limit}
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, verdict, payload, timestamp) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, db.EVENT_SENT_BACK, latest["phase"], latest["verdict"], json.dumps(payload),
             "2026-10-07T12:00:00Z"))


def event_types_since(task_id: str, mark: int) -> list[str]:
    return [event["event_type"] for event in db.get_events(settings.get_db_connection(), task_id)[mark:]]


def event_count(task_id: str) -> int:
    return len(db.get_events(settings.get_db_connection(), task_id))


class TestTasksFromBeforeThisRule:
    def test_a_ledger_written_by_the_installed_version_is_recounted(self, config, monkeypatch):
        """Nothing is converted: the same events are read by the new rule.
        The installed version counted every one of these rounds (4 here).
        The walk runs eight Builder steps, so the tripwire is set out of its
        way: this test is about the retry count."""
        monkeypatch.setattr(routing, "FIX_ROUNDS_BEFORE_THE_OWNER_DECIDES", 100)
        automatic_rounds("old", config, 1)  # an automatic round wrote then what it writes now
        for _ in range(3):
            finish_step("old", config, "approved_with_warnings")
            next_("old", config)
            mark = event_count("old")
            installed_version_send_back("old", config)
            assert event_types_since("old", mark) == ["state_saved", "retry", "sent_back"]
            assert count("old", config) == 0
            builder_finishes("old", config)
        assert len(events_of_type("old", db.EVENT_RETRY)) == 4  # what the installed version reads
        assert state_of("old", config) == ("in_progress", "reviewer")

        # Rounds nobody chose are still counted, still stop, and the stop takes a send-back.
        for round_number in (1, 2, 3):
            reviewer_asks_for_changes("old", config)
            assert count("old", config) == round_number
            builder_finishes("old", config)
        reviewer_asks_for_changes("old", config)
        assert state_of("old", config) == ("blocked", "reviewer")
        commands.cmd_send_back("old", config, note="Decided.")
        assert (state_of("old", config), count("old", config)) == (("in_progress", "builder"), 0)

    def test_a_send_back_the_installed_version_refused_reads_paused_and_moves_by_override(
            self, config, capsys):
        """Known limit: the latest verdict is one that waits at a gate, so
        routing never answers "limit", whatever the count, and the task
        reads paused. It moves the way the installed version told the owner
        to move it."""
        automatic_rounds("refused", config, 3)
        finish_step("refused", config, "approved_with_warnings")
        next_("refused", config)
        mark = event_count("refused")
        installed_version_send_back("refused", config, refused_at_the_limit=True)
        assert event_types_since("refused", mark) == ["state_saved", "sent_back"]

        assert (state_of("refused", config), count("refused", config)) == (("blocked", "reviewer"), 0)
        assert read_api.gate_check_payload("refused", config)["gate"]["kind"] == "blocked"
        assert "is blocked: it was paused." in refused(commands.cmd_send_back, "refused", config, capsys)
        commands.cmd_override("refused", "builder", config)
        assert state_of("refused", config) == ("in_progress", "builder")

    def test_an_old_blocked_task_at_the_limit_takes_a_send_back(self, config):
        """Stopped by the installed version's `relay next`: three retry
        events, then a save that set it blocked."""
        automatic_rounds("old-stop", config, 3)
        finish_step("old-stop", config, "changes_requested")
        state = run_state.load_run_state("old-stop", config)
        run_state._record_verdict_in_history(state, "reviewer", "changes_requested")
        state["status"] = "blocked"
        run_state.save_run_state("old-stop", state, config)

        gate = read_api.gate_check_payload("old-stop", config)["gate"]
        assert (gate["kind"], gate["next_phase"]) == ("retry_limit", "builder")
        commands.cmd_send_back("old-stop", config, note="Fix the last one.")
        assert (state_of("old-stop", config), count("old-stop", config)) == (
            ("in_progress", "builder"), 0)

    def test_a_paused_task_still_refuses_send_back_and_next(self, config, capsys):
        task_at("paused", config, UP_TO_REVIEW)
        finish_step("paused", config, "changes_requested")  # a loop back, under the limit
        state = run_state.load_run_state("paused", config)
        state["status"] = "blocked"
        run_state.save_run_state("paused", state, config)

        gate = read_api.gate_check_payload("paused", config)["gate"]
        assert (gate["state"], gate["kind"], gate["send_back_command"]) == (
            "waiting_on_owner", "blocked", None)
        for command in (commands.cmd_send_back, next_):
            out = refused(command, "paused", config, capsys)
            assert "Task paused is blocked: it was paused." in out
            assert f"{CLI} override paused <phase>" in out and f"{CLI} abort paused" in out
            assert "limit" not in out
        assert state_of("paused", config) == ("blocked", "reviewer")

    def test_files_backend_keeps_an_old_count_and_a_send_back_resets_it(self, tmp_path, monkeypatch):
        """The legacy backend has no events to recount, so the stored number
        stands and is treated as automatic. One send-back clears it."""
        files_config, finish = files_backend_task_in_review("legacy", tmp_path, monkeypatch)
        state = run_state.load_run_state("legacy", files_config)
        state["retries"]["builder"] = 3  # counted under the old rule, send-backs included
        run_state.save_run_state("legacy", state, files_config)

        assert count("legacy", files_config) == 3
        finish("changes_requested")
        assert state_of("legacy", files_config) == ("blocked", "reviewer")  # stopped early, and asks
        commands.cmd_send_back("legacy", files_config, note="Go on.")
        assert (state_of("legacy", files_config), count("legacy", files_config)) == (
            ("in_progress", "builder"), 0)
        assert run_state.load_run_state("legacy", files_config)["retries"]["builder"] == 0


    def test_files_backend_send_back_with_no_note_names_no_command_that_refuses_there(
            self, tmp_path, monkeypatch, capsys):
        """`relay note` needs the ledger, so on the files backend there is
        nothing to point to."""
        files_config, finish = files_backend_task_in_review("no-ledger", tmp_path, monkeypatch)
        finish("approved_with_warnings")
        capsys.readouterr()
        commands.cmd_send_back("no-ledger", files_config)
        out = capsys.readouterr().out
        assert "Sent back to builder. Automatic rounds: 0 of 3.\n" in out
        assert "No note recorded" not in out and f"{CLI} note" not in out

        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            commands.cmd_note("no-ledger", files_config, "Too late.")
        assert exit_info.value.code == 1  # the command the hint used to name


class TestWhatTheOwnerReads:
    def test_status_prints_automatic_rounds_per_phase_and_nothing_at_zero(self, config, capsys):
        task_at("words", config, [("ready-for-architect", "")])
        finish_step("words", config, "needs-designer")
        next_("words", config)                                   # an automatic round into the Designer
        finish_step("words", config, "ready-for-architect")
        next_("words", config)
        finish_step("words", config, "ready-for-builder", "deploy: no\n")
        next_("words", config)
        builder_finishes("words", config)
        reviewer_asks_for_changes("words", config)
        capsys.readouterr()
        commands.cmd_status("words", config)
        out = capsys.readouterr().out
        assert "  Automatic rounds: builder 1 of 3\n" in out
        assert "  Automatic rounds: designer 1 of 3\n" in out
        assert out.count("Automatic rounds:") == 2  # the Reviewer's count is 0: no line
        assert "Retries" not in out

        task_at("no-rounds", config, UP_TO_REVIEW)
        send_back_at_the_gate("no-rounds", config)
        capsys.readouterr()
        commands.cmd_status("no-rounds", config)
        out = capsys.readouterr().out
        assert "Automatic rounds" not in out and "Retries" not in out

    def test_status_json_counts_automatic_rounds_only_since_schema_version_2(self, config):
        automatic_rounds("json", config, 1)
        status = read_api.status_payload("json", config)
        assert (status["schema_version"], status["retries"], status["max_retries"]) == (
            3, {"builder": 1, "reviewer": 0}, 3)
        send_back_at_the_gate("json", config)
        status = read_api.status_payload("json", config)
        assert (status["schema_version"], status["retries"], status["max_retries"]) == (
            3, {"builder": 0, "reviewer": 0}, 3)

    def test_gate_check_at_the_limit_offers_send_back_before_and_after_the_stop(self, config):
        config["max_retries"] = 1
        automatic_rounds("offer", config, 1)
        finish_step("offer", config, "changes_requested")
        before = read_api.gate_check_payload("offer", config)
        next_("offer", config)
        after = read_api.gate_check_payload("offer", config)

        assert (before["status"], after["status"]) == ("in_progress", "blocked")
        assert before["gate"] == after["gate"]
        assert after["gate"] == {
            "state": "waiting_on_owner", "kind": "retry_limit", "next_phase": "builder",
            "reason": after["gate"]["reason"], "approve_command": None,
            "send_back_command": f"{CLI} send-back offer", "send_back_recommended": False}
        listed = read_api.list_payload(config)["tasks"][0]
        assert (listed["gate_state"], listed["gate_kind"]) == ("waiting_on_owner", "retry_limit")

    @pytest.mark.parametrize("next_has_stopped_it", [False, True])
    def test_a_limit_stopped_task_reads_stopped_never_paused(self, config, next_has_stopped_it):
        automatic_rounds("board", config, 3)
        finish_step("board", config, "changes_requested")
        if next_has_stopped_it:
            next_("board", config)

        rows = waiting_rows(config)
        assert rows == [("board", "Stopped after 3 automatic rounds")]
        view = view_of("board", config)
        assert view["ask"] == ("Builder went back for fixes 3 times in a row without you deciding. "
                               "Send it back for one more round, move it on, or abort.")
        words = json.dumps([rows, view])
        assert "Paused" not in words and "paused" not in words and "Retry" not in words

    @pytest.mark.parametrize("rounds, row_line, times", [
        (2, "Stopped after 2 automatic rounds", "2 times"),
        (1, "Stopped after 1 automatic round", "1 time"),
    ])
    def test_the_stop_words_give_the_rounds_that_ran_not_the_limit(
            self, config, capsys, rounds, row_line, times):
        """The limit can be lowered under a stopped task (the deploy puts 6
        back to 3). The words still say how many rounds really ran."""
        config["max_retries"] = rounds
        automatic_rounds("lowered", config, rounds)
        capsys.readouterr()
        reviewer_asks_for_changes("lowered", config)
        stopped = f"builder has gone back for fixes {times} in a row with nobody deciding"
        assert f"\n  STOPPED: {stopped} (limit {rounds}).\n" in capsys.readouterr().out

        config["max_retries"] = 0
        assert read_api.list_payload(config)["tasks"][0]["automatic_rounds"] == rounds
        assert [line for _, line in waiting_rows(config)] == [row_line]
        assert view_of("lowered", config)["ask"] == (
            f"Builder went back for fixes {times} in a row without you deciding. "
            "Send it back for one more round, move it on, or abort.")
        assert refused(next_, "lowered", config, capsys).startswith(
            f"Task lowered is stopped: {stopped} (limit 0).\n")

    def test_a_paused_task_still_reads_paused(self, config):
        task_at("held", config, UP_TO_REVIEW)
        finish_step("held", config, "changes_requested")
        state = run_state.load_run_state("held", config)
        state["status"] = "blocked"
        run_state.save_run_state("held", state, config)

        assert waiting_rows(config) == [("held", "Paused")]
        view = view_of("held", config)
        assert view["ask"] == "This task is paused. It moves again only when you override it or abort it."

    @pytest.mark.parametrize("paused", [False, True], ids=["stopped at the limit", "paused"])
    @pytest.mark.parametrize("event_type, payload", [
        (db.EVENT_SENT_BACK, "[" * 100000 + "]" * 100000),
        (db.EVENT_OWNER_NOTE, "[" * 100000 + "]" * 100000),
        (db.EVENT_SENT_BACK, "not json"),
        (db.EVENT_OWNER_NOTE, "not json"),
        (db.EVENT_TEST_RUN, "[1]"),
    ], ids=["sent_back nested too deep", "owner_note nested too deep", "sent_back not JSON",
            "owner_note not JSON", "test_run not an object"])
    def test_an_unreadable_event_on_a_blocked_task_fails_the_list_for_no_task(
            self, config, paused, event_type, payload):
        """Only a hand-edited ledger holds such a row. The task holding it
        reads as paused, as every blocked task did before the limit stop had
        a gate of its own, and the task beside it reads as it should."""
        config["max_retries"] = 1
        automatic_rounds("bad-row", config, 1)
        if paused:
            finish_step("bad-row", config, "changes_requested")
            state = run_state.load_run_state("bad-row", config)
            state["status"] = "blocked"
            run_state.save_run_state("bad-row", state, config)
            config["max_retries"] = 3
        else:
            reviewer_asks_for_changes("bad-row", config)
        task_at("beside", config, UP_TO_REVIEW)
        assert state_of("bad-row", config) == ("blocked", "reviewer")
        step = read_api.status_payload("bad-row", config)["latest_step"]["step"]
        with settings.get_db_connection() as conn:
            conn.execute(
                "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                ("bad-row", event_type, "reviewer", step, payload, "2026-10-07T12:00:00Z"))

        listed = {row["task_id"]: (row["gate_state"], row["gate_kind"])
                  for row in read_api.list_payload(config)["tasks"]}
        assert listed == {"bad-row": ("waiting_on_owner", "blocked"),
                          "beside": ("waiting_on_agent", None)}
        assert [task_id for task_id, _ in waiting_rows(config)] == ["bad-row"]


# ---------------------------------------------------------------------------
# The tripwire: a high after five fix rounds stops for the owner
# ---------------------------------------------------------------------------

THRESHOLD = 5
# The walk from the Reviewer's step to a report of each rating role.
TO_EACH_RATING_ROLE = {"reviewer": [], "tester": ["approved"], "security": ["approved", "pass"]}
HIGH = {"reviewer": "changes_requested", "tester": "fail", "security": "critical-findings"}


def fix_rounds_by_send_back(task_id: str, config: dict, rounds: int) -> None:
    """Start a task and run this many fix rounds, each one the owner's
    send-back at a warnings gate, leaving the Reviewer running."""
    task_at(task_id, config, UP_TO_REVIEW)
    for round_number in range(rounds):
        send_back_at_the_gate(task_id, config, note=f"Round {round_number + 1}.")
        builder_finishes(task_id, config)


def report_a_high(task_id: str, config: dict, phase: str) -> None:
    """From the Reviewer's step, pass on to `phase`, which reports a high."""
    for verdict in TO_EACH_RATING_ROLE[phase]:
        finish_step(task_id, config, verdict)
        next_(task_id, config)
    finish_step(task_id, config, HIGH[phase])
    next_(task_id, config)


def gate_of(task_id: str, config: dict) -> dict:
    return read_api.gate_check_payload(task_id, config)["gate"]


class TestTheFixRoundTripwire:
    @pytest.mark.parametrize("phase", list(HIGH))
    def test_a_high_after_five_fix_rounds_stops_for_the_owner(self, config, phase):
        fix_rounds_by_send_back("five", config, THRESHOLD)
        steps_before = len(steps("five", config)) + len(TO_EACH_RATING_ROLE[phase])
        report_a_high("five", config, phase)
        gate = gate_of("five", config)
        assert (gate["state"], gate["kind"], gate["next_phase"]) == (
            "waiting_on_owner", "fix_rounds", "builder")
        assert (gate["send_back_recommended"], gate["approve_command"]) == (True, None)
        assert gate["send_back_command"] == f"{CLI} send-back five"
        assert state_of("five", config) == ("in_progress", phase)
        assert len(steps("five", config)) == steps_before  # no sixth round was started
        assert gate["fix_rounds"]["count"] == THRESHOLD
        assert read_api.list_payload(config)["tasks"][0]["fix_rounds"] == THRESHOLD

    @pytest.mark.parametrize("phase", list(HIGH))
    def test_the_same_high_after_four_fix_rounds_goes_back_to_the_builder(self, config, phase):
        fix_rounds_by_send_back("four", config, THRESHOLD - 1)
        report_a_high("four", config, phase)
        assert state_of("four", config) == ("in_progress", "builder")
        assert count("four", config) == 1
        assert "fix_rounds" not in gate_of("four", config)

    def test_rounds_chosen_at_a_gate_or_by_override_or_note_count_the_same(self, config):
        task_at("mixed", config, UP_TO_REVIEW)
        send_back_at_the_gate("mixed", config, note=None)                  # round 1
        builder_finishes("mixed", config)
        finish_step("mixed", config, "approved")
        commands.cmd_override("mixed", "builder", config)                  # round 2
        builder_finishes("mixed", config)
        finish_step("mixed", config, "approved")
        commands.cmd_override("mixed", "builder", config, note="And this.")  # round 3
        builder_finishes("mixed", config)
        send_back_at_the_gate("mixed", config, note="With a note.")       # round 4
        builder_finishes("mixed", config)
        reviewer_asks_for_changes("mixed", config)                         # round 5, automatic
        builder_finishes("mixed", config)
        assert steps("mixed", config).count("builder") == THRESHOLD + 1
        reviewer_asks_for_changes("mixed", config)
        assert gate_of("mixed", config)["kind"] == "fix_rounds"
        assert state_of("mixed", config) == ("in_progress", "reviewer")

    @pytest.mark.parametrize("verdict, kind, phase", [
        ("changes_requested", "fix_rounds", "reviewer"), ("approved", None, "tester")])
    def test_after_a_send_back_a_high_after_round_six_stops_again_and_none_moves_on(
            self, config, verdict, kind, phase):
        fix_rounds_by_send_back("again", config, THRESHOLD)
        reviewer_asks_for_changes("again", config)
        assert gate_of("again", config)["kind"] == "fix_rounds"
        commands.cmd_send_back("again", config, note="One more.")         # round 6
        builder_finishes("again", config)
        finish_step("again", config, verdict)
        next_("again", config)
        assert state_of("again", config) == ("in_progress", phase)
        gate = gate_of("again", config)
        assert gate["kind"] == kind
        if kind:
            assert gate["fix_rounds"]["count"] == THRESHOLD + 1

    @pytest.mark.parametrize("walk, verdict, kind, phase", [
        ([], "approved_with_warnings", "approval", "reviewer"),
        ([], "approved", None, "tester"),
        (["approved"], "pass_with_concerns", "approval", "tester"),
        (["approved", "pass"], "findings", "approval", "security"),
        (["approved", "pass"], "secure", None, "security"),
    ])
    def test_a_medium_or_low_after_five_rounds_routes_as_before(self, config, walk, verdict,
                                                                 kind, phase):
        """A guard: these pass before the tripwire and after."""
        fix_rounds_by_send_back("calm", config, THRESHOLD)
        for earlier in walk:
            finish_step("calm", config, earlier)
            next_("calm", config)
        finish_step("calm", config, verdict)
        next_("calm", config)
        gate = gate_of("calm", config)
        assert gate["kind"] == kind and "fix_rounds" not in gate
        assert state_of("calm", config)[1] in (phase, "done")

    def test_a_failing_test_run_after_five_rounds_routes_as_before(self, config, monkeypatch):
        """A guard: a Tester's pass that Relay's own run fails goes back to
        the Builder, as an automatic round, at any round."""
        fix_rounds_by_send_back("red", config, THRESHOLD)
        finish_step("red", config, "approved")
        next_("red", config)
        monkeypatch.setattr(commands, "ensure_test_run",
                            lambda *args, **kwargs: {"outcome": "failed", "step": None,
                                                     "command": "false", "exit_code": 1})
        monkeypatch.setattr(commands.evidence, "summary", lambda run: "Relay's run failed.")
        finish_step("red", config, "pass")
        next_("red", config)
        assert state_of("red", config) == ("in_progress", "builder")
        assert count("red", config) == 1

    def test_the_retry_limit_and_the_tripwire_on_one_route_make_one_stop_with_the_list(
            self, config, capsys):
        config["max_retries"] = 1
        fix_rounds_by_send_back("both", config, THRESHOLD - 1)
        reviewer_asks_for_changes("both", config)                          # round 5, automatic
        builder_finishes("both", config)
        assert count("both", config) == 1
        capsys.readouterr()
        reviewer_asks_for_changes("both", config)
        out = capsys.readouterr().out
        assert out.count("STOPPED") == 1 and "HUMAN GATE" not in out
        assert state_of("both", config) == ("blocked", "reviewer")
        assert "5 fix rounds have run and the latest report still asks for fixes." in out
        assert f"    {CLI} send-back both --note \"what to fix\"   (recommended; " in out
        gate = gate_of("both", config)
        assert (gate["kind"], gate["send_back_recommended"], gate["fix_rounds"]["count"]) == (
            "retry_limit", True, THRESHOLD)
        out = refused(next_, "both", config, capsys)
        assert "5 fix rounds have run" in out and "(recommended; " in out
        assert read_api.list_payload(config)["tasks"][0]["fix_rounds"] == THRESHOLD

    def test_the_limit_alone_still_stops_as_before_and_lists_nothing(self, config, capsys):
        """A guard on the retry limit's own stop below the tripwire."""
        config["max_retries"] = 1
        automatic_rounds("limit", config, 1)
        reviewer_asks_for_changes("limit", config)
        gate = gate_of("limit", config)
        assert (gate["kind"], gate["send_back_recommended"]) == ("retry_limit", False)
        assert "fix_rounds" not in gate
        assert read_api.list_payload(config)["tasks"][0]["fix_rounds"] is None

    def test_fix_rounds_counts_every_builder_step_whatever_its_verdict(self):
        """A Builder step that changed nothing, or ended `needs-architect`
        or `blocked`, is a round all the same."""
        history = ([{"phase": "designer"}, {"phase": "architect"}]
                   + [{"phase": "builder", "verdict": verdict} for verdict in
                      ("ready-for-review", "needs-architect", "blocked", None)]
                   + ["not a step", {"phase": "reviewer"}])
        assert run_state.fix_rounds({"history": history}) == 3
        assert run_state.fix_rounds({"history": []}) == 0
        assert run_state.fix_rounds({}) == 0

    def test_the_legacy_pentest_verdict_is_outside_the_tripwire(self):
        """A guard: the tripwire reads the Reviewer, Tester and Security
        only, as the brief names them."""
        state = {"retries": {}, "history": [{"phase": "builder", "step": step}
                                            for step in range(THRESHOLD + 1)]}
        decision = routing.decide_routing(state, "pentest", "vulnerable", DEFAULT_CONFIG)
        assert (decision.outcome, decision.next_phase, decision.lists_fix_rounds) == (
            routing.ROUTE_ADVANCE, "builder", False)
        tripped = routing.decide_routing(state, "reviewer", "changes_requested", DEFAULT_CONFIG)
        assert (tripped.outcome, tripped.gate_kind) == (routing.ROUTE_OWNER_GATE, "fix_rounds")

    def test_relay_next_prints_the_gate_with_the_list_and_the_recommended_send_back(
            self, config, capsys):
        fix_rounds_by_send_back("printed", config, THRESHOLD)
        capsys.readouterr()
        reviewer_asks_for_changes("printed", config)
        out = before_the_stop_card(capsys.readouterr().out, "printed", config)
        artifact =read_api.status_payload("printed", config)["latest_step"]["artifact"]
        assert out.endswith(
            "\n  HUMAN GATE: fix rounds are still finding highs.\n"
            f"  Review the artifact at: {settings.TASKS_DIR / 'printed' / artifact}\n"
            "  5 fix rounds have run and the latest report still asks for fixes.\n"
            "\n  To send it back to the builder for fixes (recommended):\n"
            f"    {CLI} send-back printed\n"
            "\n  To move it somewhere else:\n"
            f"    {CLI} override printed <phase>\n"
            "\n  To stop:\n"
            f"    {CLI} abort printed\n")
        capsys.readouterr()
        commands.cmd_gate_check("printed", config)
        checked = capsys.readouterr().out
        assert "  Gate: waiting_on_owner (fix_rounds)\n" in checked
        assert "  5 fix rounds have run and the latest report still asks for fixes.\n" in checked
        assert f"  To send back (recommended): {CLI} send-back printed\n" in checked


def test_the_stop_says_how_many_fix_rounds_have_run(config, capsys):
    """The tripwire's gate says a count, in the gate's JSON, in what
    `relay next` prints, in the card's ask and in the waiting line."""
    fix_rounds_by_send_back("counted", config, THRESHOLD)
    capsys.readouterr()
    reviewer_asks_for_changes("counted", config)
    printed = capsys.readouterr().out
    sentence = "5 fix rounds have run and the latest report still asks for fixes."
    gate = read_api.gate_check_payload("counted", config)["gate"]
    assert gate["fix_rounds"] == {"count": 5, "lines": [sentence]}
    assert (gate["kind"], gate["send_back_recommended"]) == ("fix_rounds", True)
    assert f"  {sentence}\n" in printed
    assert view_of("counted", config)["ask"] == (
        f"{sentence} " + board_view._ASK_BY_KIND["fix_rounds"])
    assert waiting_rows(config) == [("counted", "Still finding highs after 5 fix rounds")]


class TestTheOwnersNumber:
    """Two fix rounds, then the owner decides."""

    def test_the_tripwire_is_set_at_two(self):
        assert routing.FIX_ROUNDS_BEFORE_THE_OWNER_DECIDES == 2

    @pytest.mark.parametrize("phase", list(HIGH))
    def test_a_high_after_two_fix_rounds_stops_for_the_owner(self, config, phase):
        fix_rounds_by_send_back("two", config, 2)
        report_a_high("two", config, phase)
        gate = gate_of("two", config)
        assert gate["kind"] == "fix_rounds"
        assert gate["fix_rounds"]["count"] == 2
        assert state_of("two", config) == ("in_progress", phase)

    @pytest.mark.parametrize("phase", list(HIGH))
    def test_a_high_after_one_fix_round_goes_back_to_the_builder(self, config, phase):
        fix_rounds_by_send_back("one", config, 1)
        report_a_high("one", config, phase)
        assert state_of("one", config) == ("in_progress", "builder")
