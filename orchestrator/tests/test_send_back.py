"""Tests for `relay send-back`: the owner returns work for fixes at a gate.

Sending work back is the recommended answer to a warnings verdict. It is not
an approval: the ledger records it as its own event. It is the owner's
decision, so it is not counted toward the retry limit (see
test_automatic_rounds.py).
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import relay
import settings
import run_state
import agents
import read_api
import commands
import gate_card
from settings import DEFAULT_CONFIG, VALID_PHASES, VALID_STATUSES, VALID_VERDICTS


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(settings, "_db_connection", None)
    db_path = tmp_path / "relay.db"
    monkeypatch.setenv("RELAY_DB_PATH", str(db_path))
    (tmp_path / "tasks").mkdir()
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    db.init_database(db_path).close()
    config = dict(DEFAULT_CONFIG)
    config["persistence_backend"] = "sqlite"
    config["human_gates"] = {name: False for name in DEFAULT_CONFIG["human_gates"]}
    yield config
    if settings._db_connection is not None:
        settings._db_connection.close()


def finish_step(task_id: str, config: dict, verdict: str, extra_frontmatter: str = "") -> None:
    """Play the agent: write the current step's artifact with this verdict."""
    latest = read_api.status_payload(task_id, config)["latest_step"]
    (settings.TASKS_DIR / task_id / latest["artifact"]).write_text(
        f"---\ntask_id: {task_id}\nphase: {latest['phase']}\nstep: {latest['step']}\n"
        f"verdict: {verdict}\n{extra_frontmatter}---\n\nBody.\n")


def task_at(task_id: str, config: dict, verdicts: list[tuple[str, str]]) -> None:
    """Start a task and walk it through these verdicts, stopping wherever `relay next` stops."""
    task_dir = settings.TASKS_DIR / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text("# Brief\n")
    commands.cmd_start(task_id, auto=False, config=config)
    for verdict, extra_frontmatter in verdicts:
        finish_step(task_id, config, verdict, extra_frontmatter)
        commands.cmd_next(task_id, auto=False, config=config)


UP_TO_REVIEW = [("ready-for-architect", ""), ("ready-for-builder", "deploy: no\n"),
                ("ready-for-review", "")]


def events_of_type(task_id: str, event_type: str) -> list[dict]:
    return [event for event in db.get_events(settings.get_db_connection(), task_id)
            if event["event_type"] == event_type]


def before_the_stop_card(out: str, task_id: str, config: dict) -> str:
    """What `relay next` printed before the gate card and stop line it ends
    every stop for the owner with (gate_card.py). Fails when they are not
    there, word for word."""
    card = "\n".join([""] + gate_card.card_lines(task_id, config, datetime.now())
                     + [gate_card.STOP_LINE]) + "\n"
    assert out.endswith(card)
    return out[:-len(card)]


class TestWarningsVerdictAtTheGate:
    def test_next_recommends_sending_back_before_accepting(self, config, capsys):
        task_at("warn", config, UP_TO_REVIEW)
        finish_step("warn", config, "approved_with_warnings")
        capsys.readouterr()
        commands.cmd_next("warn", auto=False, config=config)
        out = capsys.readouterr().out
        send_back = f"{settings._CLI_CMD} send-back warn"
        accept = f"{settings._CLI_CMD} override warn tester"
        assert "To send it back to the builder for fixes (recommended):" in out
        assert send_back in out and accept in out
        assert out.index(send_back) < out.index(accept)

    def test_gate_json_recommends_send_back(self, config):
        task_at("warn-json", config, UP_TO_REVIEW)
        finish_step("warn-json", config, "approved_with_warnings")
        gate = read_api.gate_check_payload("warn-json", config)["gate"]
        assert gate["kind"] == "approval"
        assert gate["send_back_command"] == f"{settings._CLI_CMD} send-back warn-json"
        assert gate["send_back_recommended"] is True
        assert gate["approve_command"] == f"{settings._CLI_CMD} override warn-json tester"

    @pytest.mark.parametrize("phase_verdicts, verdict, deploys, next_phase", [
        (UP_TO_REVIEW + [("approved", "")], "pass_with_concerns", "yes", "security"),
        (UP_TO_REVIEW + [("approved", "")], "pass_with_concerns", "no", "security"),
        (UP_TO_REVIEW + [("approved", ""), ("pass", "")], "findings", "yes", "ops"),
        (UP_TO_REVIEW + [("approved", ""), ("pass", "")], "findings", "no", "done"),
    ])
    def test_tester_concerns_and_security_findings_recommend_it_too(
            self, config, phase_verdicts, verdict, deploys, next_phase):
        phase_verdicts = [(v, extra.replace("deploy: no", f"deploy: {deploys}"))
                          for v, extra in phase_verdicts]
        task_at("warn-other", config, phase_verdicts)
        finish_step("warn-other", config, verdict)
        gate = read_api.gate_check_payload("warn-other", config)["gate"]
        assert (gate["state"], gate["next_phase"], gate["send_back_recommended"]) == (
            "waiting_on_owner", next_phase, True)

    def test_security_findings_on_a_non_deploy_task_can_be_sent_back(self, config):
        task_at("no-deploy-back", config, UP_TO_REVIEW + [("approved", ""), ("pass", "")])
        finish_step("no-deploy-back", config, "findings")
        commands.cmd_next("no-deploy-back", auto=False, config=config)
        commands.cmd_send_back("no-deploy-back", config, note="fix finding 2")
        status = read_api.status_payload("no-deploy-back", config)
        assert (status["task"]["status"], status["task"]["current_phase"]) == ("in_progress", "builder")
        assert events_of_type("no-deploy-back", db.EVENT_APPROVAL) == []

    def test_auto_mode_stops_at_the_findings_gate_on_a_non_deploy_task(self, config, monkeypatch):
        task_at("no-deploy-auto", config, UP_TO_REVIEW + [("approved", ""), ("pass", "")])
        finish_step("no-deploy-auto", config, "findings")
        started = []
        monkeypatch.setattr(agents, "invoke_agent",
                            lambda *args, **kwargs: started.append(args))
        commands.cmd_next("no-deploy-auto", auto=True, config=config)
        assert started == []
        assert read_api.status_payload("no-deploy-auto", config)["task"]["status"] == "in_progress"

    def test_security_findings_gate_even_when_the_task_does_not_deploy(self, config, capsys):
        task_at("no-deploy", config, UP_TO_REVIEW + [("approved", ""), ("pass", "")])
        finish_step("no-deploy", config, "findings")
        capsys.readouterr()
        commands.cmd_next("no-deploy", auto=False, config=config)
        out = capsys.readouterr().out

        # Nobody has decided about the findings yet, so the task stays open.
        assert read_api.status_payload("no-deploy", config)["task"]["status"] == "in_progress"
        accept = f"{settings._CLI_CMD} override no-deploy done"
        send_back = f"{settings._CLI_CMD} send-back no-deploy"
        assert "HUMAN GATE" in out
        assert "To accept it as it is and close the task:" in out
        assert send_back in out and accept in out
        gate = read_api.gate_check_payload("no-deploy", config)["gate"]
        assert (gate["state"], gate["kind"], gate["next_phase"]) == (
            "waiting_on_owner", "approval", "done")
        assert gate["approve_command"] == accept
        assert gate["send_back_command"] == send_back
        assert gate["send_back_recommended"] is True

        # Accepting the findings is the owner's recorded decision, and closes the task.
        commands.cmd_override("no-deploy", "done", config)
        assert read_api.status_payload("no-deploy", config)["task"]["status"] == "completed"
        assert events_of_type("no-deploy", db.EVENT_APPROVAL)[-1]["payload"]["next_phase"] == "done"

    def test_brief_gate_offers_send_back_without_recommending_it(self, config, capsys):
        config["human_gates"]["after_designer"] = True
        task_at("brief", config, [])
        finish_step("brief", config, "ready-for-architect")
        capsys.readouterr()
        commands.cmd_next("brief", auto=False, config=config)
        out = capsys.readouterr().out
        assert "To ask the designer for changes:" in out
        assert "(recommended)" not in out
        gate = read_api.gate_check_payload("brief", config)["gate"]
        assert gate["send_back_command"] == f"{settings._CLI_CMD} send-back brief"
        assert gate["send_back_recommended"] is False

    def test_no_send_back_command_when_nothing_is_waiting_on_the_owner(self, config):
        task_at("clear", config, [])
        gate = read_api.gate_check_payload("clear", config)["gate"]
        assert (gate["send_back_command"], gate["send_back_recommended"]) == (None, False)


class TestSendBack:
    def test_review_warnings_go_back_to_the_builder_as_a_fix_pass(self, config, capsys):
        task_at("fix", config, UP_TO_REVIEW)
        finish_step("fix", config, "approved_with_warnings")
        commands.cmd_next("fix", auto=False, config=config)
        capsys.readouterr()

        commands.cmd_send_back("fix", config)
        out = capsys.readouterr().out
        state = run_state.load_run_state("fix", config)
        latest = read_api.status_payload("fix", config)["latest_step"]
        assert "Sent back to builder" in out
        assert (state["status"], state["current_phase"]) == ("in_progress", "builder")
        assert (latest["phase"], latest["artifact"]) == ("builder", "05-builder-fix-1.md")
        assert state["retries"]["builder"] == 0

    def test_it_is_recorded_as_sent_back_and_never_as_an_approval(self, config):
        task_at("ledger", config, UP_TO_REVIEW)
        finish_step("ledger", config, "approved_with_warnings")
        commands.cmd_next("ledger", auto=False, config=config)
        commands.cmd_send_back("ledger", config, note="Fix the two warnings about error handling.")

        sent_back = events_of_type("ledger", db.EVENT_SENT_BACK)
        assert len(sent_back) == 1
        assert sent_back[0]["phase"] == "reviewer"
        assert sent_back[0]["verdict"] == "approved_with_warnings"
        assert sent_back[0]["payload"] == {"to_phase": "builder",
                                           "note": "Fix the two warnings about error handling."}
        assert events_of_type("ledger", db.EVENT_APPROVAL) == []

    def test_the_owners_note_reaches_the_next_agent(self, config, capsys):
        task_at("note", config, UP_TO_REVIEW)
        finish_step("note", config, "approved_with_warnings")
        commands.cmd_next("note", auto=False, config=config)
        capsys.readouterr()
        commands.cmd_send_back("note", config, note="Keep the public API unchanged.")
        out = capsys.readouterr().out
        assert "The owner sent this back with a note:\nKeep the public API unchanged." in out
        assert out.count("Keep the public API unchanged.") == 1
        assert "04-reviewer.md" in out  # the agent is pointed at what it must answer

    def test_a_brief_goes_back_to_the_designer(self, config):
        config["human_gates"]["after_designer"] = True
        task_at("revise", config, [])
        finish_step("revise", config, "ready-for-architect")
        commands.cmd_next("revise", auto=False, config=config)
        commands.cmd_send_back("revise", config, note="No PIN.")
        state = run_state.load_run_state("revise", config)
        assert state["current_phase"] == "designer"
        assert state["retries"].get("designer", 0) == 0
        assert read_api.status_payload("revise", config)["latest_step"]["step"] == 2

    def test_send_back_works_before_next_has_recorded_the_verdict(self, config):
        task_at("unrecorded", config, UP_TO_REVIEW)
        finish_step("unrecorded", config, "approved_with_warnings")  # `relay next` not run
        commands.cmd_send_back("unrecorded", config)
        history = run_state.load_run_state("unrecorded", config)["history"]
        assert [(entry["phase"], entry.get("verdict")) for entry in history[-2:]] == [
            ("reviewer", "approved_with_warnings"), ("builder", None)]

    def test_after_the_fix_the_work_is_reviewed_again(self, config):
        task_at("again", config, UP_TO_REVIEW)
        finish_step("again", config, "approved_with_warnings")
        commands.cmd_next("again", auto=False, config=config)
        commands.cmd_send_back("again", config)
        finish_step("again", config, "ready-for-review")
        commands.cmd_next("again", auto=False, config=config)
        assert run_state.load_run_state("again", config)["current_phase"] == "reviewer"


    def test_an_owner_decision_gate_goes_back_to_the_phase_that_asked(self, config, capsys):
        task_at("ask", config, [("ready-for-architect", "")])
        finish_step("ask", config, "needs-human-input")
        gate = read_api.gate_check_payload("ask", config)["gate"]
        assert (gate["kind"], gate["send_back_command"], gate["send_back_recommended"]) == (
            "owner_decision", f"{settings._CLI_CMD} send-back ask", False)
        commands.cmd_send_back("ask", config, note="Use the cheaper provider.")
        assert run_state.load_run_state("ask", config)["current_phase"] == "architect"
        assert events_of_type("ask", db.EVENT_SENT_BACK)[0]["payload"]["note"] == "Use the cheaper provider."


class TestSendBackRefusals:
    def refused(self, task_id, config, capsys):
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            commands.cmd_send_back(task_id, config)
        return exit_info.value.code, capsys.readouterr().out

    def test_nothing_to_send_back_while_the_agent_is_still_working(self, config, capsys):
        task_at("busy", config, [])
        before = read_api.status_payload("busy", config)["history"]
        code, out = self.refused("busy", config, capsys)
        assert code == 1
        assert "Nothing to send back: designer has not finished" in out
        assert read_api.status_payload("busy", config)["history"] == before

    def test_unknown_completed_and_blocked_tasks_are_refused(self, config, capsys):
        code, out = self.refused("no-such-task", config, capsys)
        assert (code, "No run state for no-such-task." in out) == (1, True)

        task_at("done", config, [])
        commands.cmd_override("done", "done", config)
        code, out = self.refused("done", config, capsys)
        assert (code, "already completed" in out) == (1, True)

        task_at("stuck", config, [])
        finish_step("stuck", config, "ready-for-architect")
        state = run_state.load_run_state("stuck", config)
        state["status"] = "blocked"
        run_state.save_run_state("stuck", state, config)
        code, out = self.refused("stuck", config, capsys)
        assert (code, "is blocked" in out) == (1, True)

    def test_refused_once_the_owner_has_approved_and_the_next_phase_started(self, config, capsys):
        """The Reviewer's warnings were accepted and the Tester is running:
        the Reviewer's verdict is no longer the owner's to send back."""
        task_at("moved-on", config, UP_TO_REVIEW)
        finish_step("moved-on", config, "approved_with_warnings")
        commands.cmd_next("moved-on", auto=False, config=config)
        commands.cmd_override("moved-on", "tester", config)
        before = (run_state.load_run_state("moved-on", config), db.get_events(settings.get_db_connection(), "moved-on"))
        code, out = self.refused("moved-on", config, capsys)
        assert (code, "Nothing to send back: tester has not finished" in out) == (1, True)
        assert (run_state.load_run_state("moved-on", config), db.get_events(settings.get_db_connection(), "moved-on")) == before

    def test_refused_when_next_would_simply_move_on(self, config, capsys):
        task_at("clean", config, UP_TO_REVIEW)
        finish_step("clean", config, "approved")
        before = db.get_events(settings.get_db_connection(), "clean")
        code, out = self.refused("clean", config, capsys)
        assert code == 1
        assert f"Nothing is waiting on you: '{settings._CLI_CMD} next clean' will move on" in out
        assert db.get_events(settings.get_db_connection(), "clean") == before

    def test_refused_at_a_gate_with_no_finished_work_to_reject(self, config, capsys):
        task_at("noflag", config, [("ready-for-architect", "")])
        finish_step("noflag", config, "ready-for-builder")  # plan with no deploy flag
        code, out = self.refused("noflag", config, capsys)
        assert (code, "send-back does not apply at this gate (deploy_flag_missing)" in out) == (1, True)

    def test_running_it_twice_records_one_step_and_one_event(self, config, capsys):
        task_at("twice", config, UP_TO_REVIEW)
        finish_step("twice", config, "approved_with_warnings")
        commands.cmd_send_back("twice", config)
        code, _ = self.refused("twice", config, capsys)
        assert code == 1
        steps = [entry["phase"] for entry in run_state.load_run_state("twice", config)["history"]]
        assert steps.count("builder") == 2
        assert len(events_of_type("twice", db.EVENT_SENT_BACK)) == 1


class TestCommandLine:
    def test_send_back_with_a_note_from_the_command_line(self, config, monkeypatch, capsys):
        task_at("cli", config, UP_TO_REVIEW)
        finish_step("cli", config, "approved_with_warnings")
        monkeypatch.setattr(settings, "load_config", lambda: config)
        monkeypatch.setattr(sys, "argv", ["relay", "send-back", "cli", "--note", "Tidy the names."])
        relay.main()
        assert run_state.load_run_state("cli", config)["current_phase"] == "builder"
        assert events_of_type("cli", db.EVENT_SENT_BACK)[0]["payload"]["note"] == "Tidy the names."

    def test_gate_check_text_shows_the_send_back_command(self, config, capsys):
        task_at("text", config, UP_TO_REVIEW)
        finish_step("text", config, "approved_with_warnings")
        capsys.readouterr()
        commands.cmd_gate_check("text", config)
        out = capsys.readouterr().out
        assert f"To send back (recommended): {settings._CLI_CMD} send-back text" in out
        assert json.loads(json.dumps(read_api.gate_check_payload("text", config)))["ok"] is True
