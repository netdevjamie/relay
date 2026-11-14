"""`relay decide`: a choice made from a task's card, by its name, for the
step and the kind of gate the card was for. It must be the same decision as the CLI command at
the Mac, recorded the same way, and it must never land on a gate that has
moved on. Also `relay next`'s stop line, and the CLI's new flags.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import db
import gate_card
import read_api
import relay
import run_state
import settings
from tests.test_automatic_rounds import builder_finishes, next_
from tests.test_gate_card import (
    GATES, SECURITY_DONE, at_accept_close, at_approval, at_deploy, at_paused,
    at_retry_limit, deploying,
)

from tests.test_send_back import UP_TO_REVIEW, config, finish_step, task_at  # noqa: F401

NOTE = "Fix the parser only."


def step_of(task_id, config) -> int:
    return read_api.status_payload(task_id, config)["latest_step"]["step"]


def events(task_id) -> list[dict]:
    return db.get_events(settings.get_db_connection(), task_id)


def recorded(task_id, config) -> tuple[list[dict], dict]:
    """The ledger's events and the run state, without times and without
    the task's own name, so two tasks walked the same way compare equal."""
    def same_for_any_task(value):
        if isinstance(value, dict):
            return {key: same_for_any_task(item) for key, item in value.items()
                    if key not in ("timestamp", "created", "updated", "task_id")}
        if isinstance(value, list):
            return [same_for_any_task(item) for item in value]
        return value.replace(task_id, "<task>") if isinstance(value, str) else value
    return (same_for_any_task(events(task_id)),
            same_for_any_task(run_state.load_run_state(task_id, config)))


def gate_kind_of(task_id, config) -> str:
    return read_api.status_payload(task_id, config)["gate"]["kind"]


def decide(task_id, config, choice, step=None, to_phase=None, note=None, gate_kind=None):
    commands.cmd_decide(task_id, choice, step_of(task_id, config) if step is None else step,
                        gate_kind_of(task_id, config) if gate_kind is None else gate_kind,
                        to_phase, note, config)


# Each case: the gate, the choice by name (with --to), and the CLI command at the Mac.
PARITY = [
    ("approval", "approve", None, lambda t, c: commands.cmd_override(t, "tester", c, note=NOTE)),
    ("approval to done", "accept-close", None, lambda t, c: commands.cmd_override(t, "done", c, note=NOTE)),
    ("deploy_approval", "deploy-done", None, lambda t, c: commands.cmd_override(t, "done", c, note=NOTE)),
    ("approval", "send-back", None, lambda t, c: commands.cmd_send_back(t, c, note=NOTE)),
    ("retry_limit", "send-back", None, lambda t, c: commands.cmd_send_back(t, c, note=NOTE)),
    ("fix_rounds", "send-back", None, lambda t, c: commands.cmd_send_back(t, c, note=NOTE)),
    ("approval", "move-to", "architect", lambda t, c: commands.cmd_override(t, "architect", c, note=NOTE)),
    ("blocked", "move-to", "builder", lambda t, c: commands.cmd_override(t, "builder", c, note=NOTE)),
    ("approval", "abort", None, lambda t, c: (commands.cmd_note(t, c, NOTE), commands.cmd_abort(t, c))),
    ("approval", "note", None, lambda t, c: commands.cmd_note(t, c, NOTE)),
]


@pytest.mark.parametrize("gate, choice, to_phase, at_the_mac", [
    pytest.param(*case, id=f"{case[1]} at {case[0]}") for case in PARITY])
def test_decide_writes_the_same_ledger_rows_as_the_cli(config, gate, choice, to_phase, at_the_mac):
    walk, _ = GATES[gate]
    walk("mac", config)
    walk("phone", config)
    assert_same(recorded("phone", config), recorded("mac", config))
    count_at_the_gate = len(events("phone"))
    at_the_mac("mac", config)
    decide("phone", config, choice, to_phase=to_phase, note=NOTE)
    assert_same(recorded("phone", config), recorded("mac", config))
    assert len(events("phone")) > count_at_the_gate


def assert_same(phone, mac) -> None:
    """Event by event, so a failure names the first row that differs."""
    (phone_events, phone_state), (mac_events, mac_state) = phone, mac
    for number, (from_phone, from_mac) in enumerate(zip(phone_events, mac_events)):
        assert from_phone == from_mac, f"event {number} differs"
    assert len(phone_events) == len(mac_events)
    assert phone_state == mac_state


def test_send_back_at_the_limit_is_a_send_back_and_starts_the_count_again_both_ways(config):
    at_retry_limit("mac", config)
    at_retry_limit("phone", config)
    commands.cmd_send_back("mac", config)
    decide("phone", config, "send-back")
    for task_id in ("mac", "phone"):
        state = run_state.load_run_state(task_id, config)
        assert (state["status"], state["current_phase"], state["retries"]["builder"]) == (
            "in_progress", "builder", 0)
        kinds = [event["event_type"] for event in events(task_id)]
        assert db.EVENT_SENT_BACK in kinds and db.EVENT_APPROVAL not in kinds


# ---------------------------------------------------------------------------
# Never on a gate that has moved on
# ---------------------------------------------------------------------------

def exits_with_one(call, *args, **kwargs):
    with pytest.raises(SystemExit) as stopped:
        call(*args, **kwargs)
    assert stopped.value.code == 1


def test_the_same_choice_twice_is_refused_the_second_time(config, capsys):
    at_approval("t", config)
    step = step_of("t", config)
    decide("t", config, "send-back", step=step)
    count = len(events("t"))
    capsys.readouterr()
    exits_with_one(decide, "t", config, "send-back", step=step)
    out = capsys.readouterr().out
    assert out.startswith(f"Not applied: t is no longer waiting at step {step}; it is now Builder, ")
    assert "t\nStep 5, Builder\n" in out and "No report yet. Nothing for you to do." in " ".join(out.split())
    assert len(events("t")) == count


@pytest.mark.parametrize("walk, choice, to_phase, refusal", [
    (at_approval, "deploy-done", None, "Not applied: deploy-done is not a choice at step 4 of t."),
    (at_deploy, "move-to", "done", "Not applied: move-to needs --to with one of: designer, "),
    (at_approval, "move-to", None, "Not applied: move-to needs --to with one of: "),
    (at_approval, "approve", "architect", "Not applied: --to is for move-to."),
    (at_approval, "note", None, "Not applied: note needs --note or --note-file."),
    (at_approval, "\x1b[31m" + "x" * 500, None, "Not applied: [31mxxx"),
    (at_approval, "", None, "Not applied:  is not a choice at step 4 of t."),
])
def test_a_choice_not_on_the_card_is_refused(config, capsys, walk, choice, to_phase, refusal):
    walk("t", config)
    before = (len(events("t")), run_state.load_run_state("t", config))
    capsys.readouterr()
    exits_with_one(decide, "t", config, choice, to_phase=to_phase)
    out = capsys.readouterr().out
    assert out.startswith(refusal)
    assert "Choices:" in out
    assert (len(events("t")), run_state.load_run_state("t", config)) == before


def test_an_old_step_is_refused_even_at_the_same_kind_of_gate(config, capsys):
    at_approval("t", config)
    old_step = step_of("t", config)
    decide("t", config, "send-back")
    builder_finishes("t", config)
    finish_step("t", config, "approved_with_warnings")
    next_("t", config)
    count = len(events("t"))
    capsys.readouterr()
    exits_with_one(decide, "t", config, "approve", step=old_step)
    assert capsys.readouterr().out.startswith(
        f"Not applied: t is no longer waiting at step {old_step}; it is now Reviewer gate.")
    assert len(events("t")) == count


def test_a_second_decision_while_the_first_is_applied_is_refused(config, capsys, monkeypatch,
                                                                 no_real_keep_awake):
    """Two decides on one gate at the same instant: the second arrives
    after the first has passed its checks and before it has recorded
    anything, which is when both would apply without a lock."""
    at_approval("t", config)
    step, kind = step_of("t", config), gate_kind_of("t", config)
    before = (len(events("t")), run_state.load_run_state("t", config), len(no_real_keep_awake))
    second = []
    send_back = commands.cmd_send_back

    def second_decision_arrives_first(task_id, config, auto=False, note=None):
        capsys.readouterr()
        exits_with_one(decide, task_id, config, "approve", step=step, gate_kind=kind)
        second.append((capsys.readouterr().out, len(events(task_id)),
                       run_state.load_run_state(task_id, config), len(no_real_keep_awake)))
        send_back(task_id, config, auto, note=note)

    monkeypatch.setattr(commands, "cmd_send_back", second_decision_arrives_first)
    decide("t", config, "send-back", step=step, gate_kind=kind)
    (out, count, state, holds), = second
    assert out.startswith("Not applied: another decision on t is under way.")
    assert "Choices:" in out
    assert (count, state, holds) == before
    kinds = [event["event_type"] for event in events("t")[before[0]:]]
    assert db.EVENT_APPROVAL not in kinds and kinds.count(db.EVENT_SENT_BACK) == 1
    assert len(no_real_keep_awake) == before[2] + 1
    assert step_of("t", config) == step + 1


@pytest.mark.parametrize("task_id", ["nope", "../t"])
def test_a_task_that_is_not_one_gets_no_lock_file(config, task_id):
    at_approval("t", config)
    with pytest.raises(read_api.TaskNotFound):
        commands.cmd_decide(task_id, "approve", 4, "approval", None, None, config)
    assert not list(settings.TASKS_DIR.parent.rglob("*.decide.lock"))


def test_relay_refusal_is_passed_through_with_the_card(config, capsys):
    at_approval("t", config)
    count = len(events("t"))
    capsys.readouterr()
    exits_with_one(decide, "t", config, "send-back", note="x" * 4001)
    out = capsys.readouterr().out
    assert out.startswith("The note is 4001 characters; the limit is 4000")
    assert "Choices:" in out
    assert len(events("t")) == count


# ---------------------------------------------------------------------------
# The note and what is printed back
# ---------------------------------------------------------------------------

AWKWARD_NOTE = 'Keep "this"  and $(echo x)\nsecond line  with 🙂'


def test_decide_note_is_recorded_as_given(config, capsys):
    at_approval("t", config)
    capsys.readouterr()
    decide("t", config, "send-back", note=AWKWARD_NOTE)
    sent = [event["payload"] for event in events("t") if event["event_type"] == db.EVENT_SENT_BACK]
    assert sent[-1]["note"] == AWKWARD_NOTE
    assert capsys.readouterr().out.endswith(f"Note: {AWKWARD_NOTE}\n")


def test_decide_without_a_note_says_no_note(config, capsys):
    at_approval("t", config)
    capsys.readouterr()
    decide("t", config, "approve")
    assert capsys.readouterr().out.endswith("\nNo note.\n")


def test_confirmation_names_decision_task_step_and_where_next(config, capsys):
    at_approval("t", config)
    step = step_of("t", config)
    capsys.readouterr()
    decide("t", config, "send-back")
    out = capsys.readouterr().out
    block = out[out.index("Recorded:"):]
    words = " ".join(block.split())
    assert gate_card.SEND_BACK_WORDS.format(role="Builder") in words
    assert f"Task t, step {step}." in words
    assert "Now: Builder, since" in words


def test_a_note_is_confirmed_as_added_not_with_its_help_text(config, capsys):
    at_approval("t", config)
    capsys.readouterr()
    decide("t", config, "note", note="Use argparse.")
    out = capsys.readouterr().out
    words = " ".join(out[out.index("Recorded:"):].split())
    assert gate_card.NOTE_ADDED_WORDS in words
    assert gate_card.NOTE_WORDS not in words


def test_approve_says_where_the_task_moves_not_that_it_overrode(config, capsys):
    at_approval("t", config)
    capsys.readouterr()
    decide("t", config, "approve")
    out = capsys.readouterr().out
    assert "Overriding" not in out


def test_move_to_names_the_phase_it_moved_to(config, capsys):
    at_approval("t", config)
    capsys.readouterr()
    decide("t", config, "move-to", to_phase="architect")
    words = " ".join(capsys.readouterr().out.split())
    assert gate_card.MOVE_TO_WORDS.format(phase="Architect") in words


# ---------------------------------------------------------------------------
# A closed task shows the time it closed
# ---------------------------------------------------------------------------

LONG_AGO = "2020-01-02T03:04:05Z"


def last_saved_long_ago(task_id, config) -> None:
    """The task as if it was last saved long before it closes, as when its
    last step started hours before the owner decides. Within one test every
    save lands in the same minute, so without this a close that keeps the
    old time would print the right one."""
    state = run_state.load_run_state(task_id, config)
    state["updated"] = LONG_AGO
    run_state.save_run_state(task_id, state, config)


def clock_times_between(start: datetime, end: datetime) -> set[str]:
    """The HH:MM a time between the two can show as today, local time."""
    return {f"{start.astimezone():%H:%M}", f"{end.astimezone():%H:%M}"}


def closed_at(task_id, config) -> datetime:
    updated = read_api.status_payload(task_id, config)["task"]["updated"]
    return datetime.fromisoformat(updated.replace("Z", "+00:00"))


def test_a_decision_that_closes_the_task_shows_the_time_it_closed(config, capsys):
    at_accept_close("t", config)
    step = step_of("t", config)
    last_saved_long_ago("t", config)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    capsys.readouterr()
    decide("t", config, "accept-close", step=step)
    recorded_line = [line for line in capsys.readouterr().out.splitlines()
                     if line.startswith("Now: ")]
    exits_with_one(decide, "t", config, "accept-close", step=step)
    refusal = capsys.readouterr().out.splitlines()[0]
    end = datetime.now(timezone.utc)
    times = clock_times_between(start, end)
    assert recorded_line[0] in {f"Now: Done {time}" for time in times}
    assert refusal in {f"Not applied: t is no longer waiting at step {step}; it is now Done {time}."
                       for time in times}
    assert start <= closed_at("t", config) <= end


@pytest.mark.parametrize("walk, verdict", [
    (SECURITY_DONE, "secure"),                                    # no deploy: Ops is skipped
    (deploying(SECURITY_DONE) + [("secure", "")], "deployed"),    # Ops shipped it
], ids=["security passes", "ops deployed"])
def test_relay_next_that_closes_the_task_records_the_time_it_closed(config, walk, verdict):
    task_at("t", config, walk)
    finish_step("t", config, verdict)
    last_saved_long_ago("t", config)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    next_("t", config)
    assert read_api.status_payload("t", config)["task"]["status"] == "completed"
    assert start <= closed_at("t", config) <= datetime.now(timezone.utc)


def test_relay_abort_records_the_time_it_was_abandoned(config):
    at_approval("t", config)
    last_saved_long_ago("t", config)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    decide("t", config, "abort")
    assert read_api.status_payload("t", config)["task"]["status"] == "abandoned"
    assert start <= closed_at("t", config) <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# relay next's stop line
# ---------------------------------------------------------------------------

def last_line(out: str) -> str:
    return [line for line in out.splitlines() if line.strip()][-1]


@pytest.mark.parametrize("gate", [
    pytest.param(name, id=name)
    for name in ("approval", "approval to done", "deploy_approval", "deploy_flag_missing",
                 "owner_decision", "retry_limit", "fix_rounds")])
def test_relay_next_ends_with_the_stop_line_at_every_owner_stop(config, capsys, gate):
    GATES[gate][0]("t", config)
    out = capsys.readouterr().out
    assert last_line(out) == gate_card.STOP_LINE
    assert out.count(gate_card.STOP_LINE) >= 1
    assert "Choices:" in out.split(gate_card.STOP_LINE)[-2]


@pytest.mark.parametrize("walk", [pytest.param(at_retry_limit, id="stopped at the limit"),
                                  pytest.param(at_paused, id="paused")])
def test_relay_next_on_a_blocked_task_ends_with_the_stop_line(config, capsys, walk):
    walk("t", config)
    capsys.readouterr()
    exits_with_one(next_, "t", config)
    out = capsys.readouterr().out
    assert last_line(out) == gate_card.STOP_LINE and "Choices:" in out


def test_relay_next_that_moves_on_has_no_stop_line(config, capsys):
    task_at("t", config, UP_TO_REVIEW)
    out = capsys.readouterr().out
    assert gate_card.STOP_LINE not in out


# ---------------------------------------------------------------------------
# The CLI's flags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["relay", "status", "t", "--step", "3"],
    ["relay", "override", "t", "tester", "--to", "builder"],
    ["relay", "decide", "t", "approve"],
    ["relay", "decide", "t", "--step", "3"],
    ["relay", "decide", "t", "approve", "--step", "3"],
    ["relay", "status", "t", "--gate", "approval"],
    ["relay", "show", "t"],
    ["relay", "waiting", "t"],
    ["relay", "decide", "t", "move-to", "--step", "3", "--gate", "approval", "--to", "nowhere"],
])
def test_cli_flags(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as stopped:
        relay.main()
    assert stopped.value.code == 2


def test_decide_through_the_cli_reads_the_note_file(config, monkeypatch, tmp_path, capsys):
    at_approval("t", config)
    note_file = tmp_path / "note.txt"
    note_file.write_text(AWKWARD_NOTE)
    monkeypatch.setattr(settings, "load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["relay", "decide", "t", "send-back", "--step",
                                      str(step_of("t", config)), "--gate", gate_kind_of("t", config),
                                      "--note-file", str(note_file)])
    capsys.readouterr()
    relay.main()
    assert capsys.readouterr().out.endswith(f"Note: {AWKWARD_NOTE}\n")
