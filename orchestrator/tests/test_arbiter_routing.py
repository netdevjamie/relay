"""The Arbiter through `relay next`: where it is called, how Relay routes
on each of its calls, the backstops that stop for the owner, every report
Relay cannot act on, the retry limit, gates left standing from before, a
second call, and what the ledger and status show.

Every test builds its own ledger in a temp folder and plays each role,
the Arbiter included, by writing its report.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arbiter
import arbiter_record
import commands
import db
import evidence
import gate_card
import read_api
import routing
import run_state
import settings
from tests.arbiter_fixtures import (
    ESCALATION, MOVE_ON, REPLAN, TARGETED, events_of, finish_arbiter, latest_step, passing_runs,
    stand_at_the_gate,
)
from tests.test_agent_dispatch import agent_name_in, claude_calls, record_commands
from tests.test_automatic_rounds import (
    HIGH, TO_EACH_RATING_ROLE, automatic_rounds, builder_finishes, next_, report_a_high,
    reviewer_asks_for_changes, send_back_at_the_gate,
)
from tests.test_send_back import UP_TO_REVIEW, config, finish_step, task_at  # noqa: F401

CLI = settings._CLI_CMD
WARNINGS = {"reviewer": "approved_with_warnings", "tester": "pass_with_concerns",
            "security": "findings"}
UP_TO_SECURITY = UP_TO_REVIEW + [("approved", ""), ("pass", "")]


@pytest.fixture(autouse=True)
def runs_pass(monkeypatch):
    """Every "ready" step gets a passing run, as a task with a test command
    does (tests/arbiter_fixtures.passing_runs)."""
    passing_runs(monkeypatch)


def gate_of(task_id, config) -> dict:
    return read_api.gate_check_payload(task_id, config)["gate"]


def status_of(task_id, config) -> dict:
    return read_api.status_payload(task_id, config)


def phases(task_id, config) -> list[str]:
    return [entry["phase"] for entry in status_of(task_id, config)["history"]]


def at_the_arbiter_after_two_loops(task_id, config, phase="reviewer") -> None:
    """Two review loops on the plan, then `phase` reports a high: the
    Arbiter is called in a loop."""
    automatic_rounds(task_id, config, 2)
    report_a_high(task_id, config, phase)
    assert latest_step(task_id, config)["phase"] == "arbiter"


def at_the_arbiter_at_a_warning(task_id, config, phase="reviewer", walk=UP_TO_REVIEW) -> None:
    """`phase` passes with something still wrong at round 0: the Arbiter is
    called at a warning gate."""
    task_at(task_id, config, walk)
    for verdict in TO_EACH_RATING_ROLE[phase]:
        finish_step(task_id, config, verdict)
        next_(task_id, config)
    finish_step(task_id, config, WARNINGS[phase])
    next_(task_id, config)
    assert latest_step(task_id, config)["phase"] == "arbiter"


def rule(task_id, config, verdict, **fields) -> None:
    finish_arbiter(task_id, config, verdict, **fields)
    next_(task_id, config)


def recommended_on_the_card(task_id, config):
    return gate_card.recommended_choice(status_of(task_id, config),
                                        gate_card.choices(status_of(task_id, config)))


# ---------------------------------------------------------------------------
# Where the Arbiter is called
# ---------------------------------------------------------------------------

class TestTheTrigger:
    @pytest.mark.parametrize("phase", list(HIGH))
    def test_a_high_after_two_review_loops_runs_the_arbiter(self, config, capsys, phase):
        automatic_rounds("t", config, 2)
        for verdict in TO_EACH_RATING_ROLE[phase]:
            finish_step("t", config, verdict)
            next_("t", config)
        finish_step("t", config, HIGH[phase])
        assert (gate_of("t", config)["state"], gate_of("t", config)["next_phase"]) == ("ready", "arbiter")
        capsys.readouterr()
        next_("t", config)
        out = capsys.readouterr().out
        assert f"{phase} verdict: {HIGH[phase]}; 2 review loops on this plan, so the Arbiter decides" in out
        latest = latest_step("t", config)
        assert (latest["phase"], latest["verdict"]) == ("arbiter", None)
        assert gate_of("t", config)["state"] == "waiting_on_agent"
        started = events_of("t", db.EVENT_ARBITER_STARTED)
        assert len(started) == 1
        assert started[0]["payload"]["situation"] == "loop"
        assert started[0]["payload"]["trigger"]["phase"] == phase

    @pytest.mark.parametrize("phase", list(HIGH))
    def test_a_high_after_one_review_loop_goes_back_to_the_builder(self, config, phase):
        automatic_rounds("t", config, 1)
        report_a_high("t", config, phase)
        assert latest_step("t", config)["phase"] == "builder"
        assert events_of("t", db.EVENT_ARBITER_STARTED) == []

    def test_a_first_high_on_a_new_plan_goes_to_the_builder_without_the_arbiter(self, config):
        """The count starts again after a re-plan (criterion 6): before the
        Arbiter, the old count tripped on this first high. (The limit on
        automatic rounds is raised out of the walk's way.)"""
        config["max_retries"] = 10
        automatic_rounds("t", config, 2)
        reviewer_asks_for_changes("t", config)        # a third high: the Arbiter
        rule("t", config, "re-plan")                   # to the Architect
        finish_step("t", config, "ready-for-builder", "deploy: no\n")
        next_("t", config)
        builder_finishes("t", config)
        reviewer_asks_for_changes("t", config)
        assert latest_step("t", config)["phase"] == "builder"
        builder_finishes("t", config)
        reviewer_asks_for_changes("t", config)
        assert latest_step("t", config)["phase"] == "builder"   # one loop on this plan so far

    @pytest.mark.parametrize("phase", list(WARNINGS))
    def test_every_warning_verdict_runs_the_arbiter_at_round_zero(self, config, phase):
        at_the_arbiter_at_a_warning("t", config, phase)
        started = events_of("t", db.EVENT_ARBITER_STARTED)
        assert [event["payload"]["situation"] for event in started] == ["warning"]
        assert phases("t", config).count("builder") == 1

    @pytest.mark.parametrize("verdict", ["pass", "pass_with_concerns"])
    def test_a_failing_test_run_goes_to_the_builder_without_the_arbiter(self, config, monkeypatch,
                                                                         verdict):
        task_at("t", config, UP_TO_REVIEW + [("approved", "")])
        monkeypatch.setattr(commands, "ensure_test_run",
                            lambda *args, **kwargs: {"outcome": "failed", "step": 5,
                                                     "command": "false", "exit_code": 1})
        monkeypatch.setattr(commands.evidence, "summary", lambda run: "Relay's run failed.")
        finish_step("t", config, verdict)
        next_("t", config)
        assert latest_step("t", config)["phase"] == "builder"
        assert events_of("t", db.EVENT_ARBITER_STARTED) == []

    def test_without_auto_next_records_the_arbiter_step_and_prints_it(self, config, capsys):
        task_at("t", config, UP_TO_REVIEW)
        finish_step("t", config, "approved_with_warnings")
        capsys.readouterr()
        next_("t", config)
        out = capsys.readouterr().out
        assert "\n  Next phase: arbiter\n  Artifact: 05-arbiter.md\n" in out
        assert "    @arbiter You are executing phase 'arbiter' for task 't'." in out
        assert f"  Then run: {CLI} next t" in out
        assert latest_step("t", config)["artifact"] == "05-arbiter.md"

    def test_with_auto_the_arbiter_is_launched_by_its_role_name(self, config, monkeypatch):
        ran = record_commands(monkeypatch)
        task_at("t", config, UP_TO_REVIEW)
        finish_step("t", config, "approved_with_warnings")
        assert commands.cmd_next("t", auto=True, config=config) == 0
        assert [agent_name_in(command) for command in claude_calls(ran)] == ["Arbiter"]


# ---------------------------------------------------------------------------
# The calls in a loop
# ---------------------------------------------------------------------------

class TestTheLoopCalls:
    def test_move_on_after_a_reviewer_high_goes_to_the_tester_and_records_its_follow_ups(
            self, config):
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "move-on")
        status = status_of("t", config)
        assert status["latest_step"]["phase"] == "tester"
        assert [(item["kind"], item["text"], item["source"]) for item in status["follow_ups"]] == [
            ("follow_up", "Reword the board line.", "arbiter"),
            ("known_flaw", "The legacy reader is slow.", "arbiter")]
        (ruled,) = events_of("t", db.EVENT_ARBITER_RULED)
        assert (ruled["verdict"], ruled["payload"]["routed"]) == ("move-on", True)
        assert events_of("t", db.EVENT_APPROVAL) == []

    def test_security_after_a_move_on_past_a_tester_fail_closes_and_never_reruns_the_tester(
            self, config):
        at_the_arbiter_after_two_loops("t", config, "tester")
        rule("t", config, "move-on")
        assert latest_step("t", config)["phase"] == "security"
        finish_step("t", config, "secure")
        next_("t", config)
        assert phases("t", config)[-3:] == ["tester", "arbiter", "security"]
        assert status_of("t", config)["task"]["status"] == "completed"

    def test_security_after_a_move_on_past_a_tester_fail_on_a_deploy_task_goes_to_ops(
            self, config):
        deploying = [(verdict, extra.replace("deploy: no", "deploy: yes"))
                     for verdict, extra in UP_TO_REVIEW]
        task_at("t", config, deploying)
        for _ in range(2):
            reviewer_asks_for_changes("t", config)
            builder_finishes("t", config)
        report_a_high("t", config, "tester")
        rule("t", config, "move-on")
        finish_step("t", config, "secure")
        next_("t", config)
        assert phases("t", config)[-4:] == ["tester", "arbiter", "security", "ops"]

    @pytest.mark.parametrize("history, weighed", [
        ([("builder", "ready-for-review"), ("tester", "fail"), ("arbiter", "move-on")], True),
        ([("builder", "ready-for-review"), ("reviewer", "changes_requested"),
          ("arbiter", "move-on")], False),
        ([("builder", "ready-for-review"), ("tester", "fail"), ("arbiter", "targeted-round")],
         False),
        ([("tester", "fail"), ("builder", "ready-for-review"), ("arbiter", "move-on")], False),
    ], ids=["move-on past a tester fail", "move-on past a reviewer high", "targeted round",
            "tester fail from before the latest build"])
    def test_only_a_move_on_past_a_tester_high_since_the_build_counts_as_the_tester(
            self, history, weighed):
        entries = [{"phase": phase, "verdict": verdict, "step": number}
                   for number, (phase, verdict) in enumerate(history, start=3)]
        assert routing._tester_ran_since_last_build({"history": entries}) is weighed

    def test_targeted_round_goes_to_the_builder_and_counts_as_an_automatic_round(self, config):
        at_the_arbiter_after_two_loops("t", config)
        before = status_of("t", config)["retries"]["builder"]
        rule("t", config, "targeted-round")
        status = status_of("t", config)
        assert status["latest_step"]["phase"] == "builder"
        assert status["retries"]["builder"] == before + 1

    @pytest.mark.parametrize("target", ["architect", "designer"])
    def test_re_plan_goes_to_the_architect_or_the_designer(self, config, target):
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "re-plan", reason="The plan is the problem.", replan_to=target)
        assert latest_step("t", config)["phase"] == target

    def test_escalate_stops_at_an_owner_gate_listed_by_waiting(self, config):
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "escalate")
        gate = gate_of("t", config)
        assert (gate["state"], gate["kind"]) == ("waiting_on_owner", "arbiter_escalation")
        assert gate["arbiter"]["recommends"] == "arbiter-1"
        waiting = gate_card.waiting_lines(config, gate_card.datetime.now())
        assert waiting[:2] == ["t", "  The Arbiter asks you"]


# ---------------------------------------------------------------------------
# The calls at a warning gate
# ---------------------------------------------------------------------------

class TestTheWarningCalls:
    def test_accept_at_a_warning_gate_moves_on(self, config):
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "move-on")
        assert latest_step("t", config)["phase"] == "tester"

    def test_targeted_round_at_a_warning_gate(self, config):
        at_the_arbiter_at_a_warning("t", config, "tester")
        rule("t", config, "targeted-round")
        assert latest_step("t", config)["phase"] == "builder"

    def test_escalation_at_a_warning_gate(self, config):
        at_the_arbiter_at_a_warning("t", config, "security", UP_TO_REVIEW)
        rule("t", config, "escalate")
        assert gate_of("t", config)["kind"] == "arbiter_escalation"

    def test_re_plan_at_a_warning_gate_stops_as_unreadable(self, config):
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "re-plan")
        gate = gate_of("t", config)
        assert gate["kind"] == "arbiter_unreadable"
        assert gate["reason"] == ("Relay could not act on the Arbiter's decision: re-plan is not "
                                  "open at a warning gate")
        assert latest_step("t", config)["phase"] == "arbiter"


def changed_command_runs(monkeypatch) -> None:
    """Relay's runs pass, and the Tester's run used a test command the base
    branch does not have."""
    def record(task_id, state, phase, verdict, step, config):
        if (phase, verdict) not in commands.VERDICTS_RELAY_TESTS:
            return None
        run = {"outcome": "passed", "command": "make test", "exit_code": 0, "commit": f"c{step}",
               "duration_seconds": 1}
        if phase == "tester":
            run.update(command="make other-test", command_differs_from_base=True,
                       base_command="make test")
        db.record_test_run(settings.get_db_connection(), task_id, phase=phase, step=step, run=run)
        return commands.latest_test_run(task_id, config, step)
    monkeypatch.setattr(commands, "ensure_test_run", record)


@pytest.mark.parametrize("ruling, then", [("move-on", "approval"), ("targeted-round", "builder")])
def test_a_warning_gate_that_also_changed_the_test_command(config, monkeypatch, capsys, ruling, then):
    """The other reason for a gate is asked again after the Arbiter rules
    (criterion 10): an accept stops at the approval gate with the ruling
    shown, and approving accepts the command; a targeted round goes to the
    Builder."""
    changed_command_runs(monkeypatch)
    at_the_arbiter_at_a_warning("t", config, "tester")
    capsys.readouterr()
    rule("t", config, ruling)
    if then == "builder":
        assert latest_step("t", config)["phase"] == "builder"
        return
    out = capsys.readouterr().out
    gate = gate_of("t", config)
    assert (gate["kind"], gate["next_phase"], gate["arbiter"]["recommends"]) == (
        "approval", "security", "accept")
    assert "This task changed how the repo is tested." in out
    assert "The Arbiter ruled to move on and recommends accepting." in out
    assert recommended_on_the_card("t", config) == "accept-and-log"   # it logs the follow-ups
    assert commands.cmd_override("t", "security", config) == 0
    (accepted,) = events_of("t", db.EVENT_TEST_COMMAND_ACCEPTED)
    assert (accepted["phase"], accepted["payload"]["command"]) == ("tester", "make other-test")


# ---------------------------------------------------------------------------
# The backstops: a move-on the owner must approve
# ---------------------------------------------------------------------------

class TestTheBackstops:
    def test_move_on_past_a_security_high_needs_the_owner(self, config):
        at_the_arbiter_after_two_loops("t", config, "security")
        rule("t", config, "move-on")
        gate = gate_of("t", config)
        assert gate["kind"] == "arbiter_needs_owner"
        assert "Security's latest report still holds a high" in gate["arbiter"]["needs_owner_because"]
        assert gate["arbiter"]["recommends"] == "accept"
        assert latest_step("t", config)["phase"] == "arbiter"

    def test_move_on_past_a_failing_test_run_needs_the_owner(self, config, monkeypatch):
        """The owner overrode past a Builder whose run failed; the Arbiter's
        move-on at the next high is the owner's to approve."""
        automatic_rounds("t", config, 2)
        reviewer_asks_for_changes("t", config)
        rule("t", config, "targeted-round")
        monkeypatch.setattr(commands, "ensure_test_run", self.failing_run)
        finish_step("t", config, "ready-for-review")
        next_("t", config)                                       # back to the Builder
        assert commands.cmd_override("t", "reviewer", config) == 0
        reviewer_asks_for_changes("t", config)
        rule("t", config, "move-on")
        gate = gate_of("t", config)
        assert gate["kind"] == "arbiter_needs_owner"
        assert gate["arbiter"]["needs_owner_because"] == ["Relay's latest test run did not pass"]
        assert gate["next_phase"] == "tester"

    @staticmethod
    def failing_run(task_id, state, phase, verdict, step, config):
        if (phase, verdict) not in commands.VERDICTS_RELAY_TESTS:
            return None
        db.record_test_run(settings.get_db_connection(), task_id, phase=phase, step=step,
                           run={"outcome": "failed", "command": "make test", "exit_code": 1,
                                "commit": "bad", "duration_seconds": 1})
        return commands.latest_test_run(task_id, config, step)

    def test_accepting_a_security_findings_on_a_deploy_task_needs_the_owner_before_ops(self, config):
        deploying = [(verdict, extra.replace("deploy: no", "deploy: yes"))
                     for verdict, extra in UP_TO_REVIEW]
        at_the_arbiter_at_a_warning("t", config, "security", deploying)
        rule("t", config, "move-on")
        gate = gate_of("t", config)
        assert (gate["kind"], gate["next_phase"]) == ("arbiter_needs_owner", "ops")
        assert gate["arbiter"]["needs_owner_because"] == ["moving on would reach Ops"]
        assert gate["approve_command"] == f"{CLI} override t ops"

    @pytest.mark.parametrize("brief, phase", [
        ("# Brief\n", "security"),
        ("---\nsize: feature\nrisky: no\ndeploy: no\n---\n# Brief\n", "reviewer"),
    ], ids=["security findings, no deploy", "work that is not risky"])
    def test_a_move_on_that_would_close_the_task_needs_the_owner(self, config, brief, phase):
        task_dir = settings.TASKS_DIR / "t"
        task_dir.mkdir(parents=True)
        (task_dir / "brief.md").write_text(brief)
        assert commands.cmd_start("t", auto=False, config=config) == 0
        first = latest_step("t", config)["phase"]
        walk = UP_TO_REVIEW if first == "designer" else UP_TO_REVIEW[1:]
        for verdict, extra in walk + [(verdict, "") for verdict in TO_EACH_RATING_ROLE[phase]]:
            finish_step("t", config, verdict, extra)
            next_("t", config)
        finish_step("t", config, WARNINGS[phase])
        next_("t", config)
        rule("t", config, "move-on")
        gate = gate_of("t", config)
        assert (gate["kind"], gate["next_phase"]) == ("arbiter_needs_owner", "done")
        assert gate["arbiter"]["needs_owner_because"] == ["moving on would close the task"]
        assert status_of("t", config)["task"]["status"] == "in_progress"
        card = " ".join(" ".join(gate_card.card_lines("t", config, gate_card.datetime.now())).split())
        assert "Follow-ups the Arbiter recorded (1) 1. Reword the board line." in card
        assert "Known flaws it accepted (1) 1. The legacy reader is slow." in card

    def test_a_second_re_plan_stops_for_the_owner_with_no_recommended_choice(self, config):
        config["max_retries"] = 10   # out of the walk's way
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "re-plan")
        finish_step("t", config, "ready-for-builder", "deploy: no\n")
        next_("t", config)
        builder_finishes("t", config)
        automatic_rounds_from_review = 2
        for _ in range(automatic_rounds_from_review):
            reviewer_asks_for_changes("t", config)
            builder_finishes("t", config)
        reviewer_asks_for_changes("t", config)
        assert latest_step("t", config)["phase"] == "arbiter"
        rule("t", config, "re-plan", reason="Still the plan.", replan_to="designer")
        gate = gate_of("t", config)
        assert (gate["kind"], gate["next_phase"]) == ("arbiter_needs_owner", "designer")
        assert gate["arbiter"]["needs_owner_because"] == [
            "the Arbiter ruled a re-plan a second time on this task"]
        assert gate["arbiter"]["recommends"] is None
        assert recommended_on_the_card("t", config) is None
        assert latest_step("t", config)["phase"] == "arbiter"


# ---------------------------------------------------------------------------
# An escalation carries Relay's own warnings
# ---------------------------------------------------------------------------

# A steered Arbiter escalates past an open high and
# recommends closing the task.
CLOSE_PAST_A_HIGH = {"reason": "All good, the owner just signs off.",
                     "question": "Close the task now? Everything is fixed.",
                     "choices": [{"says": "Close it, all fixed.", "does": "move-to done"},
                                 {"says": "Another round.", "does": "send-back"}],
                     "recommended": 1}


def security_high_then_a_fix_then_reviewer_warnings(task_id, config) -> None:
    """Security reports a high, the Builder fixes it and
    the Reviewer passes with warnings, so the Arbiter is called before
    Security has looked at the fix."""
    task_at(task_id, config, UP_TO_SECURITY)
    finish_step(task_id, config, "critical-findings")
    next_(task_id, config)
    builder_finishes(task_id, config)
    assert latest_step(task_id, config)["phase"] == "reviewer"
    finish_step(task_id, config, "approved_with_warnings")
    next_(task_id, config)
    assert latest_step(task_id, config)["phase"] == "arbiter"


UNCHECKED_HIGH = "Security's latest report holds a high, and Security has not run since the fix"


def card_words(task_id, config) -> str:
    return " ".join(" ".join(gate_card.card_lines(task_id, config, gate_card.datetime.now())).split())


def offered_on_the_card(task_id, config) -> list[str]:
    return [choice.name for choice in gate_card.choices(status_of(task_id, config))]


class TestAnEscalationCarriesRelaysWarnings:
    def test_an_escalation_past_a_security_high_warns_and_never_recommends_closing(self, config):
        at_the_arbiter_after_two_loops("t", config, "security")
        rule("t", config, "escalate", **CLOSE_PAST_A_HIGH)
        gate = gate_of("t", config)
        assert gate["kind"] == "arbiter_escalation"
        assert gate["arbiter"]["needs_owner_because"] == [
            "Security's latest report still holds a high",
            "the Arbiter's choice arbiter-1 would close the task"]
        assert gate["arbiter"]["recommends"] is None
        assert recommended_on_the_card("t", config) is None
        words = card_words("t", config)
        assert ("Relay warns: Security's latest report still holds a high; the Arbiter's choice "
                "arbiter-1 would close the task.") in words
        assert words.index("Relay warns:") < words.index("The Arbiter's reason:")
        assert "(recommended)" not in words
        offered = offered_on_the_card("t", config)
        assert offered[0] == "arbiter-2" and offered[-1] == "arbiter-1"

    def test_relay_next_says_the_warning_at_an_escalation(self, config, capsys):
        at_the_arbiter_after_two_loops("t", config, "security")
        capsys.readouterr()
        rule("t", config, "escalate", **CLOSE_PAST_A_HIGH)
        assert ("HUMAN GATE: the Arbiter asks you to decide. Relay warns: Security's latest report "
                "still holds a high; the Arbiter's choice arbiter-1 would close the task.") in (
            capsys.readouterr().out)

    def test_gate_check_says_the_warning_at_an_escalation(self, config, capsys):
        at_the_arbiter_after_two_loops("t", config, "security")
        rule("t", config, "escalate", **CLOSE_PAST_A_HIGH)
        capsys.readouterr()
        commands.cmd_gate_check("t", config)
        out = capsys.readouterr().out
        assert ("  Relay warns: Security's latest report still holds a high; the Arbiter's choice "
                "arbiter-1 would close the task.\n") in out
        assert "The Arbiter recommends" not in out

    def test_an_escalation_past_a_failing_test_run_warns_and_never_recommends_reaching_ops(
            self, config, monkeypatch):
        automatic_rounds("t", config, 2)
        reviewer_asks_for_changes("t", config)
        rule("t", config, "targeted-round")
        monkeypatch.setattr(commands, "ensure_test_run", TestTheBackstops.failing_run)
        finish_step("t", config, "ready-for-review")
        next_("t", config)                                       # back to the Builder
        assert commands.cmd_override("t", "reviewer", config) == 0
        reviewer_asks_for_changes("t", config)
        rule("t", config, "escalate", **{**CLOSE_PAST_A_HIGH, "choices": [
            {"says": "Ship it.", "does": "move-to ops"}, {"says": "Stop.", "does": "abort"}]})
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            "Relay's latest test run did not pass", "the Arbiter's choice arbiter-1 would reach Ops"]
        assert gate["arbiter"]["recommends"] is None
        assert "Relay warns: Relay's latest test run did not pass;" in card_words("t", config)
        assert offered_on_the_card("t", config)[-1] == "arbiter-1"

    def test_with_nothing_standing_a_recommended_close_stays_recommended(self, config):
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "escalate", **CLOSE_PAST_A_HIGH)
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            "the Arbiter's choice arbiter-1 would close the task"]
        assert gate["arbiter"]["recommends"] == "arbiter-1"
        assert offered_on_the_card("t", config)[0] == "arbiter-1"

    def test_a_recommended_send_back_past_a_security_high_stays_recommended(self, config):
        at_the_arbiter_after_two_loops("t", config, "security")
        rule("t", config, "escalate", **{**CLOSE_PAST_A_HIGH, "recommended": 2})
        gate = gate_of("t", config)
        assert gate["arbiter"]["recommends"] == "arbiter-2"
        assert offered_on_the_card("t", config)[0] == "arbiter-2"
        assert offered_on_the_card("t", config)[-1] == "arbiter-1"

    def test_an_escalation_offering_only_ops_and_a_close_past_a_high_recommends_neither(
            self, config):
        """Every choice the Arbiter offers leaves the task: neither is
        recommended, both come after the owner's own choices, and the card's
        own move-to does not offer the close a second time."""
        at_the_arbiter_after_two_loops("t", config, "security")
        rule("t", config, "escalate", reason="Ship.", question="Ops or close?",
             choices=[{"says": "Ship it.", "does": "move-to ops"},
                      {"says": "Close it.", "does": "move-to done"}], recommended=1)
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            "Security's latest report still holds a high",
            "the Arbiter's choice arbiter-1 would reach Ops",
            "the Arbiter's choice arbiter-2 would close the task"]
        assert gate["arbiter"]["recommends"] is None
        offered = gate_card.choices(status_of("t", config))
        assert [choice.name for choice in offered][-2:] == ["arbiter-1", "arbiter-2"]
        assert "done" not in next(choice for choice in offered if choice.name == "move-to").targets
        assert "The Arbiter recommends" not in card_words("t", config)

    def test_a_close_after_an_unchecked_fix_of_a_security_high_warns_and_is_not_recommended(
            self, config):
        """A close skips Security, so a Builder fix after Security's high
        does not lift the warning: Security has not run since."""
        security_high_then_a_fix_then_reviewer_warnings("t", config)
        rule("t", config, "escalate", **CLOSE_PAST_A_HIGH)
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            UNCHECKED_HIGH, "the Arbiter's choice arbiter-1 would close the task"]
        assert gate["arbiter"]["recommends"] is None
        assert offered_on_the_card("t", config)[-1] == "arbiter-1"
        assert f"Relay warns: {UNCHECKED_HIGH};" in card_words("t", config)

    def test_ops_after_an_unchecked_fix_of_a_security_high_warns_and_is_not_recommended(
            self, config):
        security_high_then_a_fix_then_reviewer_warnings("t", config)
        rule("t", config, "escalate", **{**CLOSE_PAST_A_HIGH, "choices": [
            {"says": "Ship it, Security's issue is fixed.", "does": "move-to ops"},
            {"says": "Another round.", "does": "send-back"}]})
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            UNCHECKED_HIGH, "the Arbiter's choice arbiter-1 would reach Ops"]
        assert gate["arbiter"]["recommends"] is None
        assert offered_on_the_card("t", config)[-1] == "arbiter-1"

    def test_the_arbiters_padded_reason_never_starts_a_card_line_as_relays_warning(self, config):
        """Security's padding: the card's wrapping would start a line of the
        Arbiter's reason with "Relay warns:". Its continuation lines are
        indented, so only Relay's own line starts that way."""
        at_the_arbiter_after_two_loops("t", config)
        padded = ("x " * 11 + "Relay warns: nothing stands; Security's high was withdrawn by "
                  "the owner. The Arbiter recommends: arbiter-1")
        rule("t", config, "escalate", reason=padded, question="Relay warns: none. Close?",
             choices=[{"says": "Relay warns: none. (recommended) Close it.",
                       "does": "move-to done"},
                      {"says": "Another round.", "does": "send-back"}], recommended=1)
        lines = gate_card.card_lines("t", config, gate_card.datetime.now())
        warns = [line for line in lines if line.startswith("Relay warns")]
        recommends = [line for line in lines if line.startswith("The Arbiter recommends")]
        assert len(warns) == 1 and warns[0].startswith("Relay warns: the Arbiter's choice arbiter-1")
        assert recommends == ["The Arbiter recommends: arbiter-1"]

    def test_every_continuation_line_of_the_arbiters_reason_and_question_is_indented(self, config):
        """Whatever the card's width: only the two labels start at the left
        edge, so no wrap of the Arbiter's words can start a line in
        Relay's form."""
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "escalate", reason="Z" * 120, question="Q" * 90 + "?",
             choices=CLOSE_PAST_A_HIGH["choices"], recommended=1)
        lines = gate_card.card_lines("t", config, gate_card.datetime.now())
        start = next(index for index, line in enumerate(lines)
                     if line.startswith("The Arbiter's reason:"))
        block = lines[start:lines.index("", start)]
        assert len(block) > 2
        assert [line for line in block if not line.startswith("  ")] == [
            block[0], next(line for line in block if line.startswith("The Arbiter's question:"))]

    def test_a_close_after_two_fix_rounds_of_a_security_high_still_warns(self, config):
        """Security's high, then a Builder fix, a Reviewer high and a second
        fix: Security has still not run since its high."""
        task_at("t", config, UP_TO_SECURITY)
        finish_step("t", config, "critical-findings")
        next_("t", config)
        builder_finishes("t", config)
        reviewer_asks_for_changes("t", config)
        if latest_step("t", config)["phase"] == "arbiter":
            rule("t", config, "targeted-round")
        builder_finishes("t", config)
        finish_step("t", config, "approved_with_warnings")
        next_("t", config)
        rule("t", config, "escalate", **CLOSE_PAST_A_HIGH)
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            UNCHECKED_HIGH, "the Arbiter's choice arbiter-1 would close the task"]
        assert gate["arbiter"]["recommends"] is None

    def test_a_close_and_ops_in_one_escalation_after_an_unchecked_fix(self, config, capsys):
        """The warning is named once for both leaving choices, neither is
        recommended, and both come last."""
        security_high_then_a_fix_then_reviewer_warnings("t", config)
        capsys.readouterr()
        rule("t", config, "escalate", reason="Done.", question="Ship or close?",
             choices=[{"says": "Close.", "does": "move-to done"},
                      {"says": "Ship.", "does": "move-to ops"},
                      {"says": "Another round.", "does": "send-back"}], recommended=2)
        assert f"Relay warns: {UNCHECKED_HIGH};" in capsys.readouterr().out
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            UNCHECKED_HIGH, "the Arbiter's choice arbiter-1 would close the task",
            "the Arbiter's choice arbiter-2 would reach Ops"]
        assert gate["arbiter"]["recommends"] is None
        offered = offered_on_the_card("t", config)
        assert offered[0] == "arbiter-3" and offered[-2:] == ["arbiter-1", "arbiter-2"]
        assert card_words("t", config).count(UNCHECKED_HIGH) == 1

    def test_a_security_report_after_its_high_lifts_the_unchecked_warning(self, config):
        """Security's high, a fix, then Security reports secure on a deploy
        task. A later fix after Ops sends it back is not an unchecked fix
        of a high: Security's latest report is not a high."""
        task_at("t", config, [("ready-for-architect", ""), ("ready-for-builder", "deploy: yes\n"),
                              ("ready-for-review", ""), ("approved", ""), ("pass", "")])
        finish_step("t", config, "critical-findings")
        next_("t", config)
        builder_finishes("t", config)
        for verdict in ("approved", "pass", "secure", "not-ready"):
            finish_step("t", config, verdict)
            next_("t", config)
        assert commands.cmd_override("t", "builder", config) == 0
        builder_finishes("t", config)
        finish_step("t", config, "approved_with_warnings")
        next_("t", config)
        rule("t", config, "escalate", reason="Done.", question="Ship or close?",
             choices=[{"says": "Close.", "does": "move-to done"},
                      {"says": "Ship.", "does": "move-to ops"}], recommended=2)
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            "the Arbiter's choice arbiter-1 would close the task",
            "the Arbiter's choice arbiter-2 would reach Ops"]
        assert gate["arbiter"]["recommends"] == "arbiter-2"

    @staticmethod
    def unchecked_harness_at_the_tester(monkeypatch) -> None:
        """Relay's run at the Tester's step passes but could not check the
        harness files (evidence-hardening's harness_unchecked)."""
        def record(task_id, state, phase, verdict, step, config):
            if (phase, verdict) not in routing.VERDICTS_RELAY_TESTS:
                return None
            run = {"outcome": "passed", "command": "make test", "exit_code": 0,
                   "commit": f"c{step:03d}", "duration_seconds": 1}
            if phase == "tester":
                run["harness_unchecked"] = "the fork point with the base was not found"
            db.record_test_run(settings.get_db_connection(), task_id, phase=phase, step=step, run=run)
            return commands.latest_test_run(task_id, config, step)
        monkeypatch.setattr(commands, "ensure_test_run", record)

    def test_a_close_past_an_unchecked_harness_names_it_and_is_not_recommended(
            self, config, monkeypatch, capsys):
        """The Tester's passing run could
        not check the harness. The escalation's close carries Relay's
        confirmation reason, is not recommended, and `relay next` prints the
        harness lines; closing after that records the acceptance."""
        self.unchecked_harness_at_the_tester(monkeypatch)
        at_the_arbiter_at_a_warning("u", config, "tester")
        capsys.readouterr()
        rule("u", config, "escalate", **CLOSE_PAST_A_HIGH)
        next_says = capsys.readouterr().out
        gate = gate_of("u", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            "Relay could not check the harness files (the fork point with the base was not "
            "found), so the owner confirms it before the passing run is believed",
            "the Arbiter's choice arbiter-1 would close the task"]
        assert gate["arbiter"]["recommends"] is None
        assert offered_on_the_card("u", config)[-1] == "arbiter-1"
        assert "Harness files not checked: the fork point with the base was not found" in next_says
        step = latest_step("u", config)["step"]
        assert commands.cmd_decide("u", "arbiter-1", step, "arbiter_escalation", None, None,
                                   config) == 0
        assert status_of("u", config)["task"]["status"] == "completed"
        assert len(events_of("u", "test_command_accepted")) == 1

    def test_an_override_past_the_report_that_no_choice_offered_accepts_nothing(
            self, config, monkeypatch, capsys):
        """The gate names the harness for its move to Security, but the
        owner overrides to Ops, which no choice offered: the move is the owner's,
        and no acceptance is recorded for it."""
        self.unchecked_harness_at_the_tester(monkeypatch)
        at_the_arbiter_at_a_warning("u", config, "tester")
        rule("u", config, "escalate", reason="Look.", question="Security?",
             choices=[{"says": "Security.", "does": "move-to security"},
                      {"says": "Another round.", "does": "send-back"}], recommended=1)
        assert gate_of("u", config)["arbiter"]["needs_owner_because"][0].startswith(
            "Relay could not check the harness files")
        capsys.readouterr()
        assert commands.cmd_override("u", "ops", config) == 0
        assert "Accepted" not in capsys.readouterr().out
        assert events_of("u", "test_command_accepted") == []

    def test_a_choice_that_goes_back_carries_no_harness_warning(self, config, monkeypatch, capsys):
        """A move back to the Reviewer is followed by the Tester's own run,
        which asks again: the harness is not named, nothing is accepted,
        and the Arbiter's recommendation stands."""
        self.unchecked_harness_at_the_tester(monkeypatch)
        at_the_arbiter_at_a_warning("u", config, "tester")
        capsys.readouterr()
        rule("u", config, "escalate", reason="Look again.", question="Reviewer?",
             choices=[{"says": "Reviewer again.", "does": "move-to reviewer"},
                      {"says": "Another round.", "does": "send-back"}], recommended=1)
        assert "Harness files not checked" not in capsys.readouterr().out
        gate = gate_of("u", config)
        assert gate["arbiter"]["needs_owner_because"] == []
        assert gate["arbiter"]["recommends"] == "arbiter-1"
        step = latest_step("u", config)["step"]
        assert commands.cmd_decide("u", "arbiter-1", step, "arbiter_escalation", None, None,
                                   config) == 0
        assert events_of("u", "test_command_accepted") == []

    @staticmethod
    def flagged_run_at_the_tester(monkeypatch, flags: dict) -> None:
        """Relay's run at the Tester's step passes, carrying `flags`."""
        def record(task_id, state, phase, verdict, step, config):
            if (phase, verdict) not in routing.VERDICTS_RELAY_TESTS:
                return None
            run = {"outcome": "passed", "command": "make test", "exit_code": 0,
                   "commit": f"c{step:03d}", "duration_seconds": 1}
            if phase == "tester":
                run.update(flags)
            db.record_test_run(settings.get_db_connection(), task_id, phase=phase, step=step, run=run)
            return commands.latest_test_run(task_id, config, step)
        monkeypatch.setattr(commands, "ensure_test_run", record)

    CHANGED_HARNESS = {"harness_changed": ["runner.py"], "harness_digest": "d1"}

    @pytest.mark.parametrize("flags", [
        {"tests_drop": {"fewer": 2, "reference": "base", "before": 9, "now": 7}, "tests_run": 7},
        {"command_differs_from_base": True, "command": "make other-test"},
        {"harness_changed": ["runner.py", "conftest.py"], "harness_digest": "d2",
         "tests_drop": {"fewer": 1, "reference": "previous run", "before": 8, "now": 7},
         "tests_run": 7, "command_differs_from_base": True, "command": "make t2",
         "harness_unchecked": "the fork point with the base was not found"},
    ], ids=["fewer-tests", "changed-command", "every-flag"])
    def test_each_flag_on_the_run_is_named_at_a_close_and_the_close_accepts_it(
            self, config, monkeypatch, capsys, flags):
        """Fewer tests run, a changed command alone, and every
        flag at once are named like a changed harness, and the close records
        an acceptance that covers the run, so it does not ask again."""
        self.flagged_run_at_the_tester(monkeypatch, flags)
        at_the_arbiter_at_a_warning("f", config, "tester")
        rule("f", config, "escalate", reason="Look.", question="Close?",
             choices=[{"says": "Close.", "does": "move-to done"},
                      {"says": "Another round.", "does": "send-back"}], recommended=1)
        trigger = gate_of("f", config)["arbiter"]["step"] - 1
        run = commands.latest_test_run("f", config, trigger)
        gate = gate_of("f", config)
        assert evidence.confirmation_reason(run) in gate["arbiter"]["needs_owner_because"]
        assert gate["arbiter"]["recommends"] is None
        capsys.readouterr()
        commands.cmd_gate_check("f", config)
        assert "A choice that moves on accepts" in capsys.readouterr().out
        assert commands.cmd_decide("f", "arbiter-1", latest_step("f", config)["step"],
                                   "arbiter_escalation", None, None, config) == 0
        assert len(events_of("f", "test_command_accepted")) == 1
        assert commands.is_test_command_confirmed("f", config, run)

    @pytest.mark.parametrize("to, past", [("done", True), ("ops", True), ("security", True),
                                          ("tester", False), ("reviewer", False),
                                          ("builder", False)])
    def test_only_a_choice_past_the_tester_names_the_harness_and_accepts_it(
            self, config, monkeypatch, to, past):
        """Each phase a choice can move to. Only a move past
        the Tester's report names the changed harness, loses the
        recommendation and records an acceptance when taken."""
        self.flagged_run_at_the_tester(monkeypatch, self.CHANGED_HARNESS)
        at_the_arbiter_at_a_warning("m", config, "tester")
        rule("m", config, "escalate", reason="Look.", question="Which?",
             choices=[{"says": "Go.", "does": f"move-to {to}"},
                      {"says": "Another round.", "does": "send-back"}], recommended=1)
        gate = gate_of("m", config)
        assert any("runner.py" in why for why in gate["arbiter"]["needs_owner_because"]) is past
        assert (gate["arbiter"]["recommends"] is None) is past
        assert commands.cmd_decide("m", "arbiter-1", latest_step("m", config)["step"],
                                   "arbiter_escalation", None, None, config) == 0
        assert len(events_of("m", "test_command_accepted")) == (1 if past else 0)

    @pytest.mark.parametrize("to", ["done", "tester", "reviewer", "builder"])
    def test_an_override_to_any_phase_no_choice_offered_accepts_nothing(
            self, config, monkeypatch, to):
        """The gate names the harness for its move to
        Security; an override anywhere else records no acceptance."""
        self.flagged_run_at_the_tester(monkeypatch, self.CHANGED_HARNESS)
        at_the_arbiter_at_a_warning("o", config, "tester")
        rule("o", config, "escalate", reason="Look.", question="Security?",
             choices=[{"says": "Security.", "does": "move-to security"},
                      {"says": "Another round.", "does": "send-back"}], recommended=1)
        assert any("runner.py" in why for why in gate_of("o", config)["arbiter"]["needs_owner_because"])
        assert commands.cmd_override("o", to, config) == 0
        assert events_of("o", "test_command_accepted") == []

    @pytest.mark.parametrize("outcome", ["not_run", "timed_out"])
    def test_an_escalation_after_a_run_that_never_finished_warns_as_after_a_failed_one(
            self, config, monkeypatch, outcome):
        """A run not run or timed out did not pass: the escalation warns and
        drops a recommended Ops choice, as after a failed run."""
        def record(task_id, state, phase, verdict, step, config):
            if (phase, verdict) not in commands.VERDICTS_RELAY_TESTS:
                return None
            db.record_test_run(settings.get_db_connection(), task_id, phase=phase, step=step,
                               run={"outcome": outcome, "command": "make test", "exit_code": None,
                                    "commit": "c", "duration_seconds": 1})
            return commands.latest_test_run(task_id, config, step)
        config["max_retries"] = 10
        automatic_rounds("t", config, 2)
        reviewer_asks_for_changes("t", config)
        rule("t", config, "targeted-round")
        monkeypatch.setattr(commands, "ensure_test_run", record)
        finish_step("t", config, "ready-for-review")
        next_("t", config)
        if latest_step("t", config)["phase"] == "builder":       # a timed-out run goes back
            assert commands.cmd_override("t", "reviewer", config) == 0
        reviewer_asks_for_changes("t", config)
        rule("t", config, "escalate", **{**CLOSE_PAST_A_HIGH, "choices": [
            {"says": "Ship it.", "does": "move-to ops"}, {"says": "Stop.", "does": "abort"}]})
        gate = gate_of("t", config)
        assert gate["arbiter"]["needs_owner_because"] == [
            "Relay's latest test run did not pass", "the Arbiter's choice arbiter-1 would reach Ops"]
        assert gate["arbiter"]["recommends"] is None
        assert offered_on_the_card("t", config)[-1] == "arbiter-1"


# ---------------------------------------------------------------------------
# A report Relay cannot act on
# ---------------------------------------------------------------------------

UNREADABLE_CHOICES = ["send-back", "ask-arbiter-again", "move-to", "abort", "note"]


def assert_stopped_unreadable(task_id, config, problem) -> None:
    status = status_of(task_id, config)
    gate = status["gate"]
    assert (gate["state"], gate["kind"]) == ("waiting_on_owner", "arbiter_unreadable")
    assert gate["reason"] == f"Relay could not act on the Arbiter's decision: {problem}"
    assert "\n" not in gate["reason"]
    assert status["latest_step"]["phase"] == "arbiter"
    assert [choice.name for choice in gate_card.choices(status)] == UNREADABLE_CHOICES
    assert recommended_on_the_card(task_id, config) is None
    card = "\n".join(gate_card.card_lines(task_id, config, gate_card.datetime.now()))
    assert "(recommended)" not in card and "recommends" not in card


class TestUnreadable:
    def test_a_missing_arbiter_report_stops_and_never_advances(self, config, capsys):
        at_the_arbiter_at_a_warning("t", config)
        assert gate_of("t", config)["state"] == "waiting_on_agent"   # it may still be running
        steps_before = phases("t", config)
        capsys.readouterr()
        next_("t", config)
        out = capsys.readouterr().out
        assert ("  HUMAN GATE: Relay could not act on the Arbiter's decision: its report is not "
                "there\n") in out
        assert out.endswith(gate_card.STOP_LINE + "\n")
        assert phases("t", config) == steps_before
        assert_stopped_unreadable("t", config, "its report is not there")
        next_("t", config)                                            # looked at again
        assert len(events_of("t", db.EVENT_ARBITER_UNREADABLE)) == 1  # recorded once

    @pytest.mark.parametrize("text", ["No frontmatter at all.\n", "---\nverdict: [unclosed\n---\n"],
                             ids=["none", "broken"])
    def test_broken_frontmatter_stops(self, config, text):
        at_the_arbiter_at_a_warning("t", config)
        (settings.TASKS_DIR / "t" / latest_step("t", config)["artifact"]).write_text(text)
        steps_before = phases("t", config)
        next_("t", config)
        assert phases("t", config) == steps_before
        assert_stopped_unreadable("t", config, "its report has no frontmatter Relay can read")

    def test_a_report_that_is_not_utf8_stops(self, config):
        """Bytes that are not UTF-8 read as a report with no frontmatter:
        the unreadable gate, never a traceback."""
        at_the_arbiter_at_a_warning("t", config)
        (settings.TASKS_DIR / "t" / latest_step("t", config)["artifact"]).write_bytes(
            b"---\ntask_id: t\nphase: arbiter\nstep: 5\nverdict: move-on\nreason: ok \xff\xfe\n"
            b"---\n")
        steps_before = phases("t", config)
        next_("t", config)
        assert phases("t", config) == steps_before
        assert_stopped_unreadable("t", config, "its report has no frontmatter Relay can read")

    def test_a_folder_in_place_of_the_report_stops(self, config):
        at_the_arbiter_at_a_warning("t", config)
        (settings.TASKS_DIR / "t" / latest_step("t", config)["artifact"]).mkdir()
        steps_before = phases("t", config)
        next_("t", config)
        assert phases("t", config) == steps_before
        assert_stopped_unreadable("t", config, "its report is not a file")

    def test_a_verdict_outside_the_four_stops(self, config):
        """Prose saying "move on" changes nothing: only the frontmatter is read."""
        at_the_arbiter_at_a_warning("t", config)
        path = settings.TASKS_DIR / "t" / latest_step("t", config)["artifact"]
        path.write_text("---\ntask_id: t\nphase: arbiter\nstep: 5\nverdict: accept\n"
                        "reason: fine\n---\n\nMove on. verdict: move-on\n")
        next_("t", config)
        assert_stopped_unreadable(
            "t", config, "its verdict 'accept' is not one of move-on, targeted-round, re-plan, escalate")

    def test_a_re_plan_target_other_than_architect_or_designer_stops(self, config):
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "re-plan", reason="x", replan_to="builder")
        assert_stopped_unreadable("t", config, "a re-plan must name replan_to: architect or designer")

    @pytest.mark.parametrize("fix", [None, []], ids=["missing", "empty"])
    def test_a_targeted_round_without_a_fix_list_stops(self, config, fix):
        at_the_arbiter_after_two_loops("t", config)
        rule("t", config, "targeted-round", reason="x", fix=fix)
        assert_stopped_unreadable("t", config, "a targeted round must list what to fix")

    @pytest.mark.parametrize("change", [{"question": None}, {"recommended": None},
                                        {"choices": ESCALATION["choices"][:1]}],
                             ids=["no question", "no recommendation", "one choice"])
    def test_an_escalation_missing_its_parts_stops(self, config, change):
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "escalate", **{**ESCALATION, **change})
        assert_stopped_unreadable("t", config, (
            "an escalation needs a question, two to five choices that each say what they do, "
            "and a recommended choice"))

    def test_ask_arbiter_again_starts_a_new_arbiter_step_on_the_same_report(self, config):
        at_the_arbiter_at_a_warning("t", config)
        next_("t", config)                                       # no report: unreadable
        assert commands.cmd_override("t", "arbiter", config) == 0
        latest = latest_step("t", config)
        assert (latest["phase"], latest["step"]) == ("arbiter", 6)
        trigger = events_of("t", db.EVENT_ARBITER_STARTED)[-1]["payload"]["trigger"]
        assert (trigger["step"], trigger["phase"]) == (4, "reviewer")

    def test_the_arbiter_cannot_be_started_anywhere_else(self, config, capsys):
        task_at("t", config, UP_TO_REVIEW)
        capsys.readouterr()
        assert commands.cmd_override("t", "arbiter", config) == 1
        assert "The Arbiter runs only where Relay calls it" in capsys.readouterr().out
        assert latest_step("t", config)["phase"] == "reviewer"


# ---------------------------------------------------------------------------
# A report changed after Relay acted on its ruling
# ---------------------------------------------------------------------------

def rulings_recorded(task_id) -> list[str]:
    return [event["verdict"] for event in events_of(task_id, db.EVENT_ARBITER_RULED)]


def rewrite_the_recorded_ruling(task_id, ruling: dict) -> None:
    """Change the ruling the ledger holds for the Arbiter's step, as only
    a hand edit of the ledger can."""
    with settings.get_db_connection() as conn:
        conn.execute("UPDATE events SET payload=? WHERE task_id=? AND event_type=?",
                     (json.dumps({"report": "05-arbiter.md", "situation": "warning",
                                  "ruling": ruling, "routed": False, "outcome": "x"}),
                      task_id, db.EVENT_ARBITER_RULED))


class TestARewrittenReport:
    """Once Relay has recorded what it did with a ruling, it routes on that
    record, as it routes every other role on the verdict it recorded: a
    report changed afterwards changes neither the route nor the card."""

    def test_a_report_rewritten_at_an_escalation_gate_stays_at_the_owners_gate(self, config):
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "escalate")
        rule("t", config, "move-on")
        assert latest_step("t", config)["phase"] == "arbiter"
        assert gate_of("t", config)["kind"] == "arbiter_escalation"
        assert rulings_recorded("t") == ["escalate"]

    def test_the_card_shows_the_recorded_escalation_not_the_rewritten_one(self, config):
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "escalate")
        finish_arbiter("t", config, "escalate", **{
            **ESCALATION, "question": "Ship it to production now?",
            "choices": [{"says": "Ship it.", "does": "move-to ops"},
                        {"says": "Stop here.", "does": "abort"}]})
        ruling = gate_of("t", config)["arbiter"]
        assert ruling["question"] == ESCALATION["question"]
        assert [choice["says"] for choice in ruling["choices"]] == [
            choice["says"] for choice in ESCALATION["choices"]]

    def test_relay_decide_after_a_rewrite_does_the_recorded_choice(self, config):
        """The owner answers the card they were shown: arbiter-1 is the
        recorded send-back, not what a rewritten report puts first."""
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "escalate")
        finish_arbiter("t", config, "escalate", **{
            **ESCALATION, "choices": [{"says": "Stop here.", "does": "abort"},
                                      {"says": "Fix it.", "does": "send-back"}]})
        assert commands.cmd_decide("t", "arbiter-1", latest_step("t", config)["step"],
                                   "arbiter_escalation", None, None, config) == 0
        assert status_of("t", config)["task"]["status"] != "abandoned"
        assert latest_step("t", config)["phase"] == "builder"

    def test_a_report_rewritten_at_a_needs_owner_gate_stays_at_the_owners_gate(self, config):
        at_the_arbiter_after_two_loops("t", config, "security")
        rule("t", config, "move-on")
        assert gate_of("t", config)["kind"] == "arbiter_needs_owner"
        rule("t", config, "targeted-round")
        assert latest_step("t", config)["phase"] == "arbiter"
        assert gate_of("t", config)["kind"] == "arbiter_needs_owner"
        assert rulings_recorded("t") == ["move-on"]

    def test_a_report_rewritten_at_an_approval_gate_after_a_move_on_stays_there(
            self, config, monkeypatch):
        changed_command_runs(monkeypatch)
        at_the_arbiter_at_a_warning("t", config, "tester")
        rule("t", config, "move-on")
        assert gate_of("t", config)["kind"] == "approval"
        rule("t", config, "targeted-round")
        assert latest_step("t", config)["phase"] == "arbiter"
        assert gate_of("t", config)["kind"] == "approval"
        assert rulings_recorded("t") == ["move-on"]

    def test_a_report_corrected_after_it_could_not_be_read_is_routed(self, config):
        """No ruling was recorded for an unreadable report, so fixing the
        report and running `relay next` again is still the way on."""
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "re-plan")
        assert gate_of("t", config)["kind"] == "arbiter_unreadable"
        rule("t", config, "move-on")
        assert latest_step("t", config)["phase"] == "tester"

    @pytest.mark.parametrize("frontmatter", [
        {"verdict": "move-on", **MOVE_ON},
        {"verdict": "targeted-round", **TARGETED, "follow_ups": ["Later."]},
        {"verdict": "re-plan", **REPLAN},
        {"verdict": "escalate", **ESCALATION},
        {"verdict": "escalate", **ESCALATION, "recommended": 2,
         "choices": [{"says": "Abort.", "does": "abort"}, {"says": "To the Tester.",
                                                           "does": "move-to tester"},
                     {"says": "A note.", "does": "note"}]},
    ], ids=["move-on", "targeted round", "re-plan", "escalate", "escalate to a phase"])
    def test_a_recorded_ruling_reads_back_as_the_ruling_it_records(self, frontmatter):
        """What the ledger keeps (ruling_record) turns back into exactly the
        ruling read from the report, every field included."""
        ruling, problem = arbiter.read_ruling(frontmatter, arbiter.SITUATION_LOOP)
        assert problem is None
        rebuilt = arbiter_record._as_frontmatter(arbiter_record.ruling_record(ruling))
        assert arbiter.read_ruling(rebuilt, arbiter.SITUATION_LOOP) == (ruling, None)

    @pytest.mark.parametrize("ruling", [
        {"decision": "move-on", "reason": ""},
        {"decision": "move-on", "reason": ["not", "text"]},
        {"decision": "targeted-round", "reason": "r", "fix": "Bound the parse."},
        {"decision": "re-plan", "reason": "r", "replan_to": "builder"},
        {"decision": "escalate", "reason": "r", "question": "q?", "recommended": "arbiter-9",
         "choices": [{"name": "arbiter-1", "says": "a", "does": "send-back", "phase": None},
                     {"name": "arbiter-2", "says": "b", "does": "abort", "phase": None}]},
        {"decision": "escalate", "reason": "r", "question": "q?", "recommended": "arbiter-1",
         "choices": [{"name": "arbiter-1", "says": "a", "does": "move-to", "phase": "arbiter"},
                     {"name": "arbiter-2", "says": "b", "does": "abort", "phase": None}]},
        {"decision": "escalate", "reason": "r", "question": "q?", "recommended": "arbiter-1",
         "choices": "send-back"},
        {"decision": "escalate", "reason": "r", "question": "q?", "recommended": "arbiter-1",
         "choices": [7, "x"]},
        {"decision": "move-on", "reason": "r", "follow_ups": "all of them"},
    ], ids=["empty reason", "reason not text", "fix not a list", "re-plan to the builder",
            "recommended choice not there", "choice moving to the arbiter", "choices not a list",
            "choices not mappings", "follow-ups not a list"])
    def test_a_recorded_ruling_relay_cannot_read_stops_and_never_advances(self, config, ruling):
        """Only a hand-edited ledger holds such a ruling. Relay neither
        routes on it nor falls back to the report."""
        at_the_arbiter_at_a_warning("t", config)
        rule("t", config, "escalate")
        rewrite_the_recorded_ruling("t", ruling)
        finish_arbiter("t", config, "move-on")   # a fallback to the report would move on
        steps_before = phases("t", config)
        next_("t", config)
        assert gate_of("t", config)["kind"] == "arbiter_unreadable"
        assert phases("t", config) == steps_before


# ---------------------------------------------------------------------------
# The retry limit, gates left standing, and a second call
# ---------------------------------------------------------------------------

def test_the_retry_limit_stops_where_the_arbiter_would_run_and_recommends_nothing(config, capsys):
    config["max_retries"] = 1
    task_at("t", config, UP_TO_REVIEW)
    send_back_at_the_gate("t", config)          # loop 1, the owner's: the count starts at 0
    builder_finishes("t", config)
    reviewer_asks_for_changes("t", config)      # loop 2, automatic: 1 of 1
    builder_finishes("t", config)
    arbiter_steps = phases("t", config).count("arbiter")
    capsys.readouterr()
    reviewer_asks_for_changes("t", config)      # where the Arbiter would run
    out = capsys.readouterr().out
    gate = gate_of("t", config)
    assert (gate["kind"], gate["send_back_recommended"], gate["review_loops"]) == (
        "retry_limit", False, 2)
    assert phases("t", config).count("arbiter") == arbiter_steps   # it did not run
    assert "  2 review loops on this plan.\n" in out
    assert "  The Arbiter last ruled (step 5): escalate.\n" in out
    assert "recommended" not in out
    assert recommended_on_the_card("t", config) is None
    assert status_of("t", config)["task"]["status"] == "blocked"


class TestStandingGates:
    """A gate a `relay next` from before the Arbiter left standing reads as
    it did (criterion 26), and the task's next report goes to the Arbiter."""

    def test_a_standing_fix_rounds_gate_stays_and_recommends_nothing(self, config):
        automatic_rounds("t", config, 2)
        finish_step("t", config, "changes_requested")
        stand_at_the_gate("t", config)
        gate = gate_of("t", config)
        assert (gate["kind"], gate["send_back_recommended"], gate["fix_rounds"]["count"]) == (
            "fix_rounds", False, 2)
        assert recommended_on_the_card("t", config) is None
        next_("t", config)
        assert latest_step("t", config)["phase"] == "reviewer"   # no Arbiter step

    def test_a_standing_warning_gate_stays_an_approval_gate(self, config):
        task_at("t", config, UP_TO_REVIEW)
        finish_step("t", config, "approved_with_warnings")
        stand_at_the_gate("t", config)
        gate = gate_of("t", config)
        assert (gate["kind"], gate["next_phase"], gate["send_back_recommended"]) == (
            "approval", "tester", True)
        next_("t", config)
        assert latest_step("t", config)["phase"] == "reviewer"

    def test_a_standing_gates_next_qualifying_report_goes_to_the_arbiter(self, config):
        task_at("t", config, UP_TO_REVIEW)
        finish_step("t", config, "approved_with_warnings")
        stand_at_the_gate("t", config)
        assert commands.cmd_send_back("t", config, note="Fix it.") == 0
        builder_finishes("t", config)
        finish_step("t", config, "approved_with_warnings")
        next_("t", config)
        assert latest_step("t", config)["phase"] == "arbiter"


def test_a_second_call_is_routed_with_its_own_ruling(config):
    config["max_retries"] = 10   # out of the walk's way
    at_the_arbiter_after_two_loops("t", config)
    rule("t", config, "targeted-round")
    builder_finishes("t", config)
    reviewer_asks_for_changes("t", config)
    assert latest_step("t", config)["phase"] == "arbiter"
    rule("t", config, "move-on")
    assert latest_step("t", config)["phase"] == "tester"
    assert [event["verdict"] for event in events_of("t", db.EVENT_ARBITER_RULED)] == [
        "targeted-round", "move-on"]


# ---------------------------------------------------------------------------
# The ledger and what the owner reads
# ---------------------------------------------------------------------------

def test_the_ledger_and_status_show_every_ruling(config, capsys):
    config["max_retries"] = 10   # out of the walk's way
    at_the_arbiter_after_two_loops("t", config)
    rule("t", config, "targeted-round")
    builder_finishes("t", config)
    reviewer_asks_for_changes("t", config)
    rule("t", config, "escalate")
    rulings = status_of("t", config)["arbiter_rulings"]
    assert [(ruling["step"], ruling["report"], ruling["situation"], ruling["decision"],
             ruling["reason"], ruling["replan_to"], ruling["routed"]) for ruling in rulings] == [
        (9, "09-arbiter.md", "loop", "targeted-round", "One real problem remains.", None, True),
        (12, "12-arbiter.md", "loop", "escalate", "The owner must choose.", None, False)]
    assert rulings[0]["trigger"] == {"step": 8, "phase": "reviewer", "verdict": "changes_requested"}
    steps = status_of("t", config)["builder_steps"]
    assert (steps["task"]["review_loop"], steps["task"]["review_loop_by_starter"]["arbiter"]) == (3, 1)
    capsys.readouterr()
    commands.cmd_status("t", config)
    out = capsys.readouterr().out
    assert "\n  The Arbiter: 2 ruling(s)\n    step 12 (loop): escalate\n" in out
    assert "    Its reason (the Arbiter's words): The owner must choose.\n" in out
    assert "\n  Builder steps:\n    for the task: 4 in all: 3 review loops" in out


def test_an_arbiter_ruling_is_never_an_approval(config):
    at_the_arbiter_after_two_loops("t", config)
    rule("t", config, "move-on")
    assert status_of("t", config)["approvals"] == []
    assert events_of("t", db.EVENT_APPROVAL) == []


def test_files_backend_routes_on_the_ruling_and_keeps_no_events(tmp_path, monkeypatch):
    """The legacy backend routes from the files; it keeps no rulings. No
    ledger is used: the path given is one that does not exist."""
    from tests.test_automatic_rounds import files_backend_task_in_review
    monkeypatch.setenv("RELAY_DB_PATH", str(tmp_path / "no-ledger.db"))
    monkeypatch.setattr(settings, "_db_connection", None)
    files_config, finish = files_backend_task_in_review("f", tmp_path, monkeypatch)
    finish("approved_with_warnings")
    state = run_state.load_run_state("f", files_config)
    assert state["history"][-1]["phase"] == "arbiter"
    finish("move-on", "reason: Cosmetic.\n")
    state = run_state.load_run_state("f", files_config)
    assert state["history"][-1]["phase"] == "tester"
    assert read_api.status_payload("f", files_config)["arbiter_rulings"] == []


@pytest.mark.parametrize("outcome", ["not_run", "timed_out", "failed"])
def test_a_latest_run_that_did_not_pass_includes_one_that_never_ran(outcome):
    """For the move-on backstop, a run that was not run counts as not
    passing (a known limit: such a task sends every move-on to the owner)."""
    import routing
    history = [{"phase": "builder", "step": 3, "verdict": "ready-for-review"},
               {"phase": "reviewer", "step": 4, "verdict": "approved_with_warnings"},
               {"phase": "arbiter", "step": 5, "verdict": "move-on"}]
    decision = routing.decide_routing(
        {"history": history, "retries": {}}, "arbiter", "move-on", settings.DEFAULT_CONFIG,
        arbiter_report={"verdict": "move-on", "reason": "Fine."},
        latest_task_test_run={"outcome": outcome, "step": 3})
    assert (decision.gate_kind, decision.needs_owner_because) == (
        "arbiter_needs_owner", ("Relay's latest test run did not pass",))
    passed = routing.decide_routing(
        {"history": history, "retries": {}}, "arbiter", "move-on", settings.DEFAULT_CONFIG,
        arbiter_report={"verdict": "move-on", "reason": "Fine."},
        latest_task_test_run={"outcome": "passed", "step": 3})
    assert (passed.outcome, passed.next_phase) == ("advance", "tester")


@pytest.mark.parametrize("event_type, payload", [
    (db.EVENT_ARBITER_RULED, '{"report": [1, 2], "ruling": [1], "routed": "yes"}'),
    (db.EVENT_ARBITER_RULED, '{"ruling": "move-on", "routed": true}'),
    (db.EVENT_ARBITER_RULED, '{"ruling": {"decision": "ship", "follow_ups": "all"}, "routed": true}'),
    (db.EVENT_ARBITER_UNREADABLE, '{"problem": {"a": 1}, "report": null}'),
    (db.EVENT_FOLLOW_UPS_LOGGED, '{"items": 5}'),
    (db.EVENT_FOLLOW_UPS_LOGGED, '{"items": [{"text": 7}, "x", {"text": "kept"}]}'),
    (db.EVENT_ARBITER_STARTED, '{"trigger": [1]}'),
], ids=["ruled fields of the wrong types", "ruling not a mapping", "ruling with a bad decision",
        "unreadable fields of the wrong types", "items not a list", "items of the wrong types",
        "started with a bad trigger"])
def test_a_hand_edited_arbiter_event_breaks_no_reader(config, event_type, payload):
    """Only a hand-edited ledger holds such a row. Status, the card and the
    accepted items still read; what cannot be read is left out."""
    at_the_arbiter_at_a_warning("t", config)
    with settings.get_db_connection() as conn:
        conn.execute("INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) "
                     "VALUES (?,?,?,?,?,?)", ("t", event_type, "arbiter", 5, payload,
                                              "2026-11-20T12:00:00Z"))
    status = status_of("t", config)
    assert all(ruling["decision"] is None for ruling in status["arbiter_rulings"])
    assert all(isinstance(ruling[key], (str, type(None)))
               for ruling in status["arbiter_rulings"] for key in ("report", "problem", "reason"))
    assert status["gate"]["state"] == "waiting_on_agent"   # no readable problem was recorded
    assert [item["text"] for item in status["follow_ups"]] in ([], ["kept"])
    gate_card.card_lines("t", config, gate_card.datetime.now())
    commands.cmd_status("t", config)


def test_send_back_from_an_unreadable_ruling_goes_to_the_builder(config, capsys):
    at_the_arbiter_at_a_warning("t", config)
    next_("t", config)                                       # no report: unreadable
    capsys.readouterr()
    assert commands.cmd_send_back("t", config, note="Do the second warning.") == 0
    out = capsys.readouterr().out
    assert latest_step("t", config)["phase"] == "builder"
    assert (f"sent back by the owner, from the Arbiter's report at {settings.TASKS_DIR / 't'}"
            "/05-arbiter.md, which could not be read.") in out
    assert f"Answer what {settings.TASKS_DIR / 't' / '04-reviewer.md'} says" in out
    (sent,) = events_of("t", db.EVENT_SENT_BACK)
    assert (sent["phase"], sent["verdict"], sent["payload"]["to_phase"]) == ("arbiter", None, "builder")
