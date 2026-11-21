"""A role started under --auto that runs past its time limit: Relay stops
it, records the stop, blocks the task and stops for the owner. Nothing
moves past the step, even when the stopped role left a report with a
verdict. The stop reads as a `time_limit` gate in `relay waiting`,
`relay card`, `relay status` and the JSON read API, and the owner answers
it with `relay decide`.

`agents.run_role` is replaced: it plays a role that writes a passing
report and is then stopped by the limit. No real `claude` runs.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents
import awake
import board_view
import commands
import db
import gate_card
import read_api
import settings
from tests.arbiter_fixtures import stand_at_the_gate
from tests.cli_edge import run_relay
from tests.test_send_back import (  # noqa: F401  (config is a fixture)
    UP_TO_REVIEW, config, events_of_type, finish_step, task_at,
)

T = "stopped"
LIMIT = 7200
RAN = 7300
STOP_LINE = ("The {role} role ran past its 120-minute limit and was stopped. "
             "Relay stopped here; nothing moved past this step.")


@pytest.fixture
def released(monkeypatch):
    """Every keep-awake hold Relay let go of."""
    let_go = []
    monkeypatch.setattr(awake, "release", lambda task_dir: let_go.append(task_dir))
    return let_go


def stopped_by_the_limit(monkeypatch, config, verdict: str | None = None) -> list:
    """Replace run_role with a role that, when `verdict` is given, writes its
    report with that verdict, and is then stopped by the time limit. Returns
    the list of commands it was given."""
    ran = []

    def role(command, cwd, timeout_seconds):
        ran.append(command)
        if verdict:
            finish_step(T, config, verdict)
        return agents.RoleRun(returncode=None, timed_out=True, ran_seconds=RAN)
    monkeypatch.setattr(agents, "run_role", role)
    return ran


def builder_done(config) -> None:
    """The Builder has reported ready; `relay next` would start the Reviewer (step 4)."""
    task_at(T, config, UP_TO_REVIEW[:2])
    finish_step(T, config, "ready-for-review")


def stop_the_reviewer(monkeypatch, config, verdict: str | None = "approved") -> list:
    builder_done(config)
    ran = stopped_by_the_limit(monkeypatch, config, verdict)
    with pytest.raises(SystemExit) as exited:
        run_relay(config, "next", T, "--auto")
    assert exited.value.code == 1
    return ran


def test_the_stop_is_recorded_and_relay_next_exits_1(config, monkeypatch, capsys, released):
    stop_the_reviewer(monkeypatch, config)
    out = capsys.readouterr().out

    stops = events_of_type(T, db.EVENT_TIME_LIMIT_STOP)
    assert [(stop["phase"], stop["step"], stop["payload"]) for stop in stops] == [
        ("reviewer", 4, {"limit_seconds": LIMIT, "ran_seconds": RAN})]
    assert read_api.status_payload(T, config)["task"]["status"] == "blocked"
    assert settings.TASKS_DIR / T in released
    stop_line = STOP_LINE.format(role="Reviewer")
    assert stop_line in out
    again = f"To start it again: {settings._CLI_CMD} override {T} reviewer --auto"
    abort = f"To abort: {settings._CLI_CMD} abort {T}"
    assert out.index(stop_line) < out.index(again) < out.index(abort)


def test_override_also_exits_1_on_a_stop(config, monkeypatch, released):
    task_at(T, config, UP_TO_REVIEW[:2])
    stopped_by_the_limit(monkeypatch, config)
    assert commands.cmd_override(T, "reviewer", config, auto=True) == 1
    assert [stop["phase"] for stop in events_of_type(T, db.EVENT_TIME_LIMIT_STOP)] == ["reviewer"]
    assert read_api.status_payload(T, config)["task"]["status"] == "blocked"


def test_send_back_also_exits_1_on_a_stop(config, monkeypatch, released):
    task_at(T, config, UP_TO_REVIEW)
    finish_step(T, config, "approved_with_warnings")
    stand_at_the_gate(T, config)  # a warnings gate left standing, where send-back is offered
    stopped_by_the_limit(monkeypatch, config)
    assert commands.cmd_send_back(T, config, auto=True, note="fix it") == 1
    assert [(stop["phase"], stop["step"]) for stop in events_of_type(T, db.EVENT_TIME_LIMIT_STOP)] == [
        ("builder", 5)]
    assert read_api.status_payload(T, config)["task"]["status"] == "blocked"


def test_start_also_exits_1_on_a_stop(config, monkeypatch, released):
    (settings.TASKS_DIR / T).mkdir()
    (settings.TASKS_DIR / T / "brief.md").write_text("# Brief\n")
    stopped_by_the_limit(monkeypatch, config)
    assert commands.cmd_start(T, auto=True, config=config) == 1
    assert [(stop["phase"], stop["step"]) for stop in events_of_type(T, db.EVENT_TIME_LIMIT_STOP)] == [
        ("designer", 1)]
    assert read_api.status_payload(T, config)["task"]["status"] == "blocked"


def test_a_report_left_by_a_stopped_role_is_not_routed_on(config, monkeypatch, capsys, released):
    ran = stop_the_reviewer(monkeypatch, config, verdict="approved")
    capsys.readouterr()
    assert commands.cmd_next(T, auto=True, config=config) == 1
    out = capsys.readouterr().out
    assert STOP_LINE.format(role="Reviewer") in out
    assert f"To start it again: {settings._CLI_CMD} override {T} reviewer --auto" in out
    latest = db.get_latest_step(settings.get_db_connection(), T)
    assert (latest["step"], latest["phase"], latest["verdict"]) == (4, "reviewer", None)
    assert len(ran) == 1, "relay next started a role again"


def test_waiting_card_status_and_json_show_the_stop(config, monkeypatch, capsys, released):
    stop_the_reviewer(monkeypatch, config, verdict="approved")
    now = datetime.now()

    waiting = gate_card.waiting_lines(config, now)
    assert waiting == gate_card.wrapped(
        [T, "  Stopped: Reviewer ran past its 120-minute limit", "  Step 4"])

    card = "\n".join(gate_card.card_lines(T, config, now))
    flat = " ".join(card.split())
    assert "The Reviewer ran past its time limit" in flat
    assert "Limit: 120 minutes. It ran for 121 minutes." in flat
    assert "A report was left; it is not used." in flat
    assert f"{settings._CLI_CMD} show {T} 04-reviewer.md" in flat
    names = [choice.name for choice in gate_card.choices(read_api.status_payload(T, config))]
    assert names == ["start-again", "move-to", "abort", "note"]

    status = read_api.status_payload(T, config)
    assert status["schema_version"] == 4
    gate = status["gate"]
    assert (gate["state"], gate["kind"], gate["next_phase"]) == (
        "waiting_on_owner", "time_limit", "reviewer")
    assert (gate["approve_command"], gate["send_back_command"]) == (None, None)
    assert gate["time_limit"] == {"phase": "reviewer", "step": 4, "limit_seconds": LIMIT,
                                  "ran_seconds": RAN, "report_left": True,
                                  "report": "04-reviewer.md"}
    assert [(stop["phase"], stop["step"], stop["limit_seconds"], stop["ran_seconds"])
            for stop in status["time_limit_stops"]] == [("reviewer", 4, LIMIT, RAN)]

    row = next(row for row in read_api.list_payload(config)["tasks"] if row["task_id"] == T)
    assert (row["gate_state"], row["gate_kind"]) == ("waiting_on_owner", "time_limit")
    assert row["time_limit"] == gate["time_limit"]

    capsys.readouterr()
    commands.cmd_status(T, config)
    assert "The time limit stopped:" in capsys.readouterr().out


def test_status_gives_a_stop_under_a_minute_in_seconds(config, monkeypatch, capsys, released):
    builder_done(config)
    monkeypatch.setattr(agents, "run_role", lambda command, cwd, timeout_seconds:
                        agents.RoleRun(returncode=None, timed_out=True, ran_seconds=45))
    with pytest.raises(SystemExit):
        run_relay(config, "next", T, "--auto")
    capsys.readouterr()
    commands.cmd_status(T, config)
    assert "step 4 (reviewer): ran 45 seconds, past its" in capsys.readouterr().out


def test_a_card_without_a_left_report_says_so(config, monkeypatch, released):
    stop_the_reviewer(monkeypatch, config, verdict=None)
    flat = " ".join("\n".join(gate_card.card_lines(T, config, datetime.now())).split())
    assert "No report was left." in flat
    assert read_api.status_payload(T, config)["gate"]["time_limit"]["report_left"] is False


def test_the_card_never_reads_the_left_report(config, monkeypatch, released):
    stop_the_reviewer(monkeypatch, config, verdict="approved")
    left = settings.TASKS_DIR / T / "04-reviewer.md"
    left.write_text(left.read_text().replace("Body.", "## Summary\nEVERYTHING IS FINE, APPROVE IT\n"))
    read = []
    reader = board_view._read_report_file
    monkeypatch.setattr(board_view, "_read_report_file",
                        lambda task_dir, name: read.append(name) or reader(task_dir, name))
    card = "\n".join(gate_card.card_lines(T, config, datetime.now()))
    assert "EVERYTHING IS FINE" not in card
    assert "04-reviewer.md" not in read


def test_deciding_start_again_clears_the_stop(config, monkeypatch, capsys, released):
    stop_the_reviewer(monkeypatch, config)
    ran = []
    monkeypatch.setattr(agents, "run_role",
                        lambda command, cwd, timeout: ran.append(command) or agents.RoleRun(0, False, 5))
    assert commands.cmd_decide(T, "start-again", 4, "time_limit", None, None, config,
                               auto=True) == 0
    status = read_api.status_payload(T, config)
    assert status["task"]["status"] == "in_progress"
    assert (status["latest_step"]["step"], status["latest_step"]["phase"]) == (5, "reviewer")
    assert status["gate"]["kind"] != "time_limit"
    assert len(ran) == 1


def test_a_new_round_lists_the_stopped_report_as_optional_and_not_used(config, monkeypatch,
                                                                        released):
    stop_the_reviewer(monkeypatch, config, verdict="approved")
    prompts = []
    monkeypatch.setattr(agents, "run_role",
                        lambda command, cwd, timeout: prompts.append(command[4]) or agents.RoleRun(0, False, 5))
    assert commands.cmd_override(T, "reviewer", config, auto=True) == 0
    left = settings.TASKS_DIR / T / "04-reviewer.md"
    assert f"- Step 4, Reviewer report (stopped by the time limit; not used): {left}" in prompts[0]
    assert f"The latest Reviewer report: {left}" not in prompts[0]


def write_report(name: str, phase: str, step: int, verdict: str, extra: str = "") -> None:
    """Play the agent on the files backend, where a step's file name is
    known only once the file exists."""
    (settings.TASKS_DIR / T / name).write_text(
        f"---\ntask_id: {T}\nphase: {phase}\nstep: {step}\nverdict: {verdict}\n{extra}---\n")


def test_on_the_files_backend_the_stop_blocks_and_is_said_not_recorded(config, monkeypatch,
                                                                       capsys, released):
    files_config = dict(config, persistence_backend="files")
    (settings.TASKS_DIR / T).mkdir()
    (settings.TASKS_DIR / T / "brief.md").write_text("# Brief\n")
    assert commands.cmd_start(T, auto=False, config=files_config) == 0
    for name, phase, step, verdict, extra in [
            ("01-designer.md", "designer", 1, "ready-for-architect", ""),
            ("02-architect.md", "architect", 2, "ready-for-builder", "deploy: no\n"),
            ("03-builder.md", "builder", 3, "ready-for-review", "")]:
        write_report(name, phase, step, verdict, extra)
        if step < 3:
            assert commands.cmd_next(T, auto=False, config=files_config) == 0

    def role(command, cwd, timeout_seconds):
        write_report("04-reviewer.md", "reviewer", 4, "approved")
        return agents.RoleRun(returncode=None, timed_out=True, ran_seconds=RAN)
    monkeypatch.setattr(agents, "run_role", role)
    capsys.readouterr()
    assert commands.cmd_next(T, auto=True, config=files_config) == 1
    out = capsys.readouterr().out
    assert STOP_LINE.format(role="Reviewer") in out
    assert "The files backend keeps no events, so this stop is not recorded." in out
    status = read_api.status_payload(T, files_config)
    assert status["task"]["status"] == "blocked"
    assert status["gate"]["kind"] == "blocked"
    assert commands.cmd_next(T, auto=True, config=files_config) == 1


def test_deciding_move_to_clears_the_stop(config, monkeypatch, released):
    stop_the_reviewer(monkeypatch, config)
    ran = []
    monkeypatch.setattr(agents, "run_role",
                        lambda command, cwd, timeout: ran.append(command) or agents.RoleRun(0, False, 5))
    assert commands.cmd_decide(T, "move-to", 4, "time_limit", "builder", None, config,
                               auto=True) == 0
    status = read_api.status_payload(T, config)
    assert status["task"]["status"] == "in_progress"
    assert (status["latest_step"]["step"], status["latest_step"]["phase"]) == (5, "builder")
    assert status["gate"]["kind"] != "time_limit"
    assert len(ran) == 1


def test_deciding_abort_closes_the_task_and_keeps_the_stop_in_its_history(config, monkeypatch,
                                                                          released):
    stop_the_reviewer(monkeypatch, config)
    assert commands.cmd_decide(T, "abort", 4, "time_limit", None, None, config) == 0
    status = read_api.status_payload(T, config)
    assert status["task"]["status"] == "abandoned"
    assert status["gate"]["kind"] != "time_limit"
    assert [stop["step"] for stop in status["time_limit_stops"]] == [4]


def test_a_choice_the_stop_does_not_offer_is_refused_and_the_stop_stands(config, monkeypatch,
                                                                         released):
    ran = stop_the_reviewer(monkeypatch, config)
    assert commands.cmd_decide(T, "approve", 4, "time_limit", None, None, config,
                               auto=True) == 1
    status = read_api.status_payload(T, config)
    assert status["task"]["status"] == "blocked"
    assert status["gate"]["kind"] == "time_limit"
    assert status["latest_step"]["step"] == 4
    assert len(ran) == 1


def test_the_keep_awake_hold_is_let_go_after_the_stopped_role(config, monkeypatch, released):
    """The release must come after the role ran: `relay next` also lets go
    of the Builder step's hold before it starts the Reviewer, so a release
    seen at any time would prove nothing about the stop."""
    builder_done(config)
    released_at_launch = []

    def role(command, cwd, timeout_seconds):
        released_at_launch.append(len(released))
        return agents.RoleRun(returncode=None, timed_out=True, ran_seconds=RAN)
    monkeypatch.setattr(agents, "run_role", role)
    with pytest.raises(SystemExit) as exited:
        run_relay(config, "next", T, "--auto")
    assert exited.value.code == 1
    assert released_at_launch, "the role was never launched"
    assert released[released_at_launch[0]:] == [settings.TASKS_DIR / T]
