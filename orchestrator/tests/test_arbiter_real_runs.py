"""The Arbiter with Relay's own test run in the path: a real git repo, a
small test runner and Relay's checks of counts and harness files. No
stand-in runs. Every test builds its own ledger and repo in a temp folder.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import db
import evidence
import gate_card
import read_api
import settings
from tests.arbiter_fixtures import finish_arbiter, latest_step
from tests.git_fixtures import cases_repo, commit_all, git, write_cases  # noqa: F401  (a fixture)
from tests.test_evidence import UP_TO_BUILD, task_in_repo
from tests.test_send_back import config, finish_step  # noqa: F401  (a fixture)

UNCHECKED_HIGH = "Security's latest report holds a high, and Security has not run since the fix"


def finish(task_id: str, config: dict, verdict: str) -> None:
    finish_step(task_id, config, verdict)
    assert commands.cmd_next(task_id, auto=False, config=config) == 0


def rule(task_id: str, config: dict, verdict: str, **fields) -> None:
    finish_arbiter(task_id, config, verdict, **fields)
    assert commands.cmd_next(task_id, auto=False, config=config) == 0


def runs(task_id: str, config: dict) -> list[dict]:
    return read_api.status_payload(task_id, config)["test_runs"]


def gate_of(task_id: str, config: dict) -> dict:
    return read_api.gate_check_payload(task_id, config)["gate"]


def on_a_branch_with_a_new_test(repo: Path, name: str) -> None:
    git(repo, "checkout", "-q", "-b", "relay/task")
    write_cases(repo, name, {f"{name}_one": "pass"})
    commit_all(repo, "build")


def concerns_at_the_tester_after_the_runner_changed(task_id: str, config: dict, repo: Path) -> None:
    """Build and review, then the branch changes the test runner and the
    Tester reports pass_with_concerns: Relay's run passes with a changed
    harness, and the Arbiter is called."""
    task_in_repo(task_id, config, repo, UP_TO_BUILD)
    on_a_branch_with_a_new_test(repo, "test_b")
    finish(task_id, config, "ready-for-review")
    finish(task_id, config, "approved")
    runner = repo / "runner.py"
    runner.write_text(runner.read_text() + "\n# changed\n")
    commit_all(repo, "change the runner")
    finish(task_id, config, "pass_with_concerns")
    assert latest_step(task_id, config)["phase"] == "arbiter"


def acceptances(task_id: str) -> list[dict]:
    return [event for event in db.get_events(settings.get_db_connection(), task_id)
            if event["event_type"] == "test_command_accepted"]


def test_an_escalation_after_an_unchecked_fix_of_a_security_high_then_a_walk_to_done(
        config, cases_repo, capsys):
    """Security's high, a fix and the Reviewer's warnings, with Relay's real
    runs: the escalation warns and recommends neither leaving choice. The
    owner sends it back, and the task walks on to done through Security."""
    task_in_repo("e", config, cases_repo, UP_TO_BUILD)
    on_a_branch_with_a_new_test(cases_repo, "test_b")
    for verdict in ("ready-for-review", "approved", "pass", "critical-findings"):
        finish("e", config, verdict)
    write_cases(cases_repo, "test_c", {"test_c_one": "pass"})
    commit_all(cases_repo, "fix the high")
    finish("e", config, "ready-for-review")
    finish("e", config, "approved_with_warnings")
    capsys.readouterr()
    rule("e", config, "escalate", reason="Done.", question="Ship or close?",
         choices=[{"says": "Close.", "does": "move-to done"},
                  {"says": "Ship.", "does": "move-to ops"},
                  {"says": "Another round.", "does": "send-back"}], recommended=1)
    assert f"Relay warns: {UNCHECKED_HIGH};" in capsys.readouterr().out
    assert [(run["phase"], run["outcome"], run["tests_run"]) for run in runs("e", config)] == [
        ("builder", "passed", 4), ("tester", "passed", 4), ("builder", "passed", 5)]
    assert runs("e", config)[-1]["commit"] == git(cases_repo, "rev-parse", "HEAD")
    arbiter = gate_of("e", config)["arbiter"]
    assert arbiter["needs_owner_because"][0] == UNCHECKED_HIGH
    assert arbiter["recommends"] is None
    assert gate_card.recommended_choice(read_api.status_payload("e", config),
                                        gate_card.choices(read_api.status_payload("e", config))) is None
    step = latest_step("e", config)["step"]
    assert commands.cmd_decide("e", "arbiter-3", step, "arbiter_escalation", None, None, config) == 0
    assert latest_step("e", config)["phase"] == "builder"
    write_cases(cases_repo, "test_d", {"test_d_one": "pass"})
    commit_all(cases_repo, "another round")
    for verdict in ("ready-for-review", "approved", "pass", "secure"):
        finish("e", config, verdict)
    assert read_api.status_payload("e", config)["task"]["status"] == "completed"
    assert [run["tests_run"] for run in runs("e", config)][-2:] == [6, 6]


def test_a_move_on_past_a_tester_warning_whose_run_changed_the_harness_stops_for_approval(
        config, cases_repo, capsys):
    """The runner changes after the build. Relay's run at the Tester's
    pass_with_concerns sees it, and the Arbiter's move-on stops at the
    approval gate naming the harness file; approving goes on to Security."""
    task_in_repo("h", config, cases_repo, UP_TO_BUILD)
    on_a_branch_with_a_new_test(cases_repo, "test_b")
    finish("h", config, "ready-for-review")
    finish("h", config, "approved")
    runner = cases_repo / "runner.py"
    runner.write_text(runner.read_text() + "\n# changed\n")
    commit_all(cases_repo, "change the runner")
    finish("h", config, "pass_with_concerns")
    assert latest_step("h", config)["phase"] == "arbiter"
    capsys.readouterr()
    rule("h", config, "move-on")
    out = capsys.readouterr().out
    assert runs("h", config)[-1]["harness_changed"] == ["runner.py"]
    gate = gate_of("h", config)
    assert (gate["kind"], gate["next_phase"]) == ("approval", "security")
    assert latest_step("h", config)["phase"] == "arbiter"
    assert "This task changed how the repo is tested." in out and "runner.py" in out
    assert commands.cmd_override("h", "security", config) == 0
    assert latest_step("h", config)["phase"] == "security"


def test_an_escalation_close_past_a_changed_harness_names_it_and_recommends_nothing(
        config, cases_repo, capsys):
    """The branch changed the runner, and
    the Arbiter escalates with a recommended close. Relay's gate names the
    harness in its own words and drops the recommendation; `relay next` and
    `gate-check` print how testing changed. Closing after that warning
    records the owner's acceptance."""
    concerns_at_the_tester_after_the_runner_changed("h", config, cases_repo)
    capsys.readouterr()
    rule("h", config, "escalate", reason="Tests pass.", question="Close?",
         choices=[{"says": "Close, tests pass.", "does": "move-to done"},
                  {"says": "Another round.", "does": "send-back"}], recommended=1)
    next_says = capsys.readouterr().out
    run = runs("h", config)[-1]
    assert run["harness_changed"] == ["runner.py"]
    arbiter = gate_of("h", config)["arbiter"]
    assert evidence.confirmation_reason(run) in arbiter["needs_owner_because"]
    assert arbiter["recommends"] is None
    assert "This task changed how the repo is tested." in next_says
    assert "Harness files changed against the base: runner.py" in next_says
    assert "A choice that moves on accepts what is listed above for this task" in next_says
    commands.cmd_gate_check("h", config)
    gate_check_says = capsys.readouterr().out
    assert "This task changed how the repo is tested." in gate_check_says
    assert "Harness files changed against the base: runner.py" in gate_check_says
    step = latest_step("h", config)["step"]
    assert commands.cmd_decide("h", "arbiter-1", step, "arbiter_escalation", None, None, config) == 0
    assert read_api.status_payload("h", config)["task"]["status"] == "completed"
    assert len(acceptances("h")) == 1


def test_an_escalation_to_security_past_a_changed_harness_names_it_before_accepting(
        config, cases_repo, capsys):
    """The escalation's move-to security
    accepts the harness only because its gate named it first."""
    concerns_at_the_tester_after_the_runner_changed("s", config, cases_repo)
    rule("s", config, "escalate", reason="Tests pass.", question="Security next?",
         choices=[{"says": "Let Security look.", "does": "move-to security"},
                  {"says": "Another round.", "does": "send-back"}], recommended=1)
    arbiter = gate_of("s", config)["arbiter"]
    assert evidence.confirmation_reason(runs("s", config)[-1]) in arbiter["needs_owner_because"]
    assert arbiter["recommends"] is None
    step = latest_step("s", config)["step"]
    capsys.readouterr()
    assert commands.cmd_decide("s", "arbiter-1", step, "arbiter_escalation", None, None, config) == 0
    assert "Accepted this task's harness files as they are at" in capsys.readouterr().out
    assert latest_step("s", config)["phase"] == "security"
    assert len(acceptances("s")) == 1


def test_an_override_at_an_escalation_that_never_named_the_harness_accepts_nothing(
        config, cases_repo, capsys):
    """The escalation offers
    no choice that moves on, so its gate never names the harness. The
    owner's own override to Security, the phase a clean pass would reach,
    records no acceptance of a harness the owner was never shown."""
    concerns_at_the_tester_after_the_runner_changed("n", config, cases_repo)
    rule("n", config, "escalate", reason="Unsure.", question="Another round?",
         choices=[{"says": "Another round.", "does": "send-back"},
                  {"says": "Stop.", "does": "abort"}], recommended=1)
    assert gate_of("n", config)["arbiter"]["needs_owner_because"] == []
    capsys.readouterr()
    assert commands.cmd_override("n", "security", config) == 0
    assert "Accepted" not in capsys.readouterr().out
    assert acceptances("n") == []


def test_an_override_straight_after_a_security_high_still_warns_at_a_later_escalation(
        config, cases_repo):
    """The owner overrides to the Builder
    straight after Security's high, before any `relay next` read it. The
    override records the report's verdict, so the escalation after the fix
    still warns that Security has not run since, and recommends neither
    leaving choice."""
    task_in_repo("o", config, cases_repo, UP_TO_BUILD)
    on_a_branch_with_a_new_test(cases_repo, "test_b")
    for verdict in ("ready-for-review", "approved", "pass"):
        finish("o", config, verdict)
    finish_step("o", config, "critical-findings")
    assert commands.cmd_override("o", "builder", config) == 0
    write_cases(cases_repo, "test_c", {"test_c_one": "pass"})
    commit_all(cases_repo, "fix the high")
    finish("o", config, "ready-for-review")
    finish("o", config, "approved_with_warnings")
    rule("o", config, "escalate", reason="Done.", question="Ship or close?",
         choices=[{"says": "Close.", "does": "move-to done"},
                  {"says": "Ship.", "does": "move-to ops"},
                  {"says": "Another round.", "does": "send-back"}], recommended=1)
    arbiter = gate_of("o", config)["arbiter"]
    assert arbiter["needs_owner_because"][0] == UNCHECKED_HIGH
    assert arbiter["recommends"] is None
    security = [entry for entry in read_api.status_payload("o", config)["history"]
                if entry["phase"] == "security"]
    assert security[-1].get("verdict") == "critical-findings"


def test_an_override_straight_after_a_security_high_still_stops_a_later_move_on(
        config, cases_repo):
    """The same override, to the Tester with no fix between: Security's high
    stands since the latest build, so the Arbiter's move-on after the
    Tester's warnings stops for the owner and says why."""
    task_in_repo("m", config, cases_repo, UP_TO_BUILD)
    on_a_branch_with_a_new_test(cases_repo, "test_b")
    for verdict in ("ready-for-review", "approved", "pass"):
        finish("m", config, verdict)
    finish_step("m", config, "critical-findings")
    assert commands.cmd_override("m", "tester", config) == 0
    finish("m", config, "pass_with_concerns")
    assert latest_step("m", config)["phase"] == "arbiter"
    rule("m", config, "move-on")
    gate = gate_of("m", config)
    assert gate["kind"] == "arbiter_needs_owner"
    assert "Security's latest report still holds a high" in gate["arbiter"]["needs_owner_because"]
    assert latest_step("m", config)["phase"] == "arbiter"


def test_an_override_before_relay_next_read_an_escalation_accepts_nothing(
        config, cases_repo, capsys):
    """The escalation offers no choice
    that moves on, and `relay next` has not read it. The report is then
    rewritten to offer move-to security, and the owner's own override to
    Security records no acceptance: only a ruling Relay recorded counts."""
    concerns_at_the_tester_after_the_runner_changed("f", config, cases_repo)
    finish_arbiter("f", config, "escalate", reason="Unsure.", question="Another round?",
                   choices=[{"says": "Another round.", "does": "send-back"},
                            {"says": "Stop.", "does": "abort"}], recommended=1)
    assert gate_of("f", config)["arbiter"]["needs_owner_because"] == []
    finish_arbiter("f", config, "escalate", reason="Tests pass.", question="Security next?",
                   choices=[{"says": "Let Security look.", "does": "move-to security"},
                            {"says": "Another round.", "does": "send-back"}], recommended=1)
    capsys.readouterr()
    assert commands.cmd_override("f", "security", config) == 0
    assert "Accepted" not in capsys.readouterr().out
    assert acceptances("f") == []
    assert latest_step("f", config)["phase"] == "security"


def test_an_override_before_relay_next_read_a_move_on_accepts_nothing(config, cases_repo, capsys):
    """The same window for a move-on: the report is rewritten from an
    escalation to a move-on before `relay next` read it, and the owner's
    override to Security, where the move-on goes, records no acceptance."""
    concerns_at_the_tester_after_the_runner_changed("v", config, cases_repo)
    finish_arbiter("v", config, "escalate", reason="Unsure.", question="Another round?",
                   choices=[{"says": "Another round.", "does": "send-back"},
                            {"says": "Stop.", "does": "abort"}], recommended=1)
    finish_arbiter("v", config, "move-on")
    capsys.readouterr()
    assert commands.cmd_override("v", "security", config) == 0
    assert "Accepted" not in capsys.readouterr().out
    assert acceptances("v") == []
