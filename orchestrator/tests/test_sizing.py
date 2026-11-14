"""The chain is sized to the task. A brief's
frontmatter gives `size` (fix, feature, project) and `risky` (yes, no);
a brief that gives neither runs the full chain."""

import pytest

import commands
import read_api
import routing
import settings
import sizing
from tests.test_send_back import config, finish_step  # noqa: F401


# ---------------------------------------------------------------------------
# What a brief says
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("---\nsize: fix\nrisky: no\ndeploy: no\n---\n# Fix\n", ("fix", False, None, False)),
    ("---\nsize: feature\nrisky: yes\ndeploy: yes\n---\n", ("feature", True, None, True)),
    ("---\nsize: project\nrisky: false\n---\n", ("project", False, None, None)),
])
def test_a_brief_that_says_all_it_must_is_read_as_written(text, expected):
    got = sizing.sizing_in(text)
    assert (got.size, got.risky, got.note, got.deploy) == expected


@pytest.mark.parametrize("text", [
    "# Brief with no frontmatter\n",
    "---\nsize: tiny\nrisky: no\n---\n",
    "---\nsize: fix\nrisky: maybe\n---\n",
    "---\nsize: fix\n---\n",
    "---\nsize: big\nrisky: no\n---\n",
    "---\nsize: fix\nrisky: no\n---\n",
    "---\nsize: feature\nrisky: no\ndeploy: maybe\n---\n",
    "---\n[not, a, mapping]\n---\n",
    "---\nsize: [fix\n---\n",
])
def test_a_brief_missing_or_garbling_any_field_runs_the_whole_full_chain(text):
    got = sizing.sizing_in(text)
    assert got.note
    assert (got.size, got.risky) == ("project", True)


def test_a_missing_brief_runs_the_full_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path)
    got = sizing.read_sizing("nowhere")
    assert (got.size, got.risky) == ("project", True) and got.note


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

FEATURE_SAFE = sizing.Sizing("feature", False)
FEATURE_RISKY = sizing.Sizing("feature", True)
PROJECT_SAFE = sizing.Sizing("project", False)
NO_GATES = {"human_gates": {"after_designer": False}, "max_retries": 2}


def decide(phase, verdict, size, state=None):
    return routing.decide_routing(state or {"history": []}, phase, verdict, NO_GATES, sizing=size)


def test_a_designer_that_needs_no_architect_goes_to_the_builder():
    decision = decide("designer", "ready-for-builder", FEATURE_SAFE)
    assert (decision.outcome, decision.next_phase) == (routing.ROUTE_ADVANCE, "builder")


def test_a_project_always_gets_the_architect():
    decision = decide("designer", "ready-for-builder", PROJECT_SAFE)
    assert decision.next_phase == "architect"
    assert "always gets the Architect" in decision.reason


def test_the_brief_gate_stands_whichever_way_the_designer_goes():
    gated = {"human_gates": {"after_designer": True}, "max_retries": 2}
    for verdict in ("ready-for-architect", "ready-for-builder"):
        decision = routing.decide_routing({"history": []}, "designer", verdict, gated,
                                          sizing=FEATURE_SAFE)
        assert decision.outcome == routing.ROUTE_OWNER_GATE


def test_work_that_is_not_risky_ends_its_checks_at_the_reviewer():
    decision = decide("reviewer", "approved", FEATURE_SAFE)
    assert decision.outcome == routing.ROUTE_COMPLETE_SKIPPING_OPS
    assert "not risky" in decision.reason


def test_work_that_is_not_risky_and_deploys_goes_from_the_reviewer_to_ops():
    decision = decide("reviewer", "approved", FEATURE_SAFE, {"history": [], "deploy_intent": True})
    assert (decision.outcome, decision.next_phase) == (routing.ROUTE_ADVANCE, "ops")


def test_warnings_on_work_that_is_not_risky_still_stop_for_the_owner():
    decision = decide("reviewer", "approved_with_warnings", FEATURE_SAFE)
    assert (decision.outcome, decision.next_phase) == (routing.ROUTE_OWNER_GATE, "done")


def test_risky_work_still_goes_to_the_tester_and_security():
    assert decide("reviewer", "approved", FEATURE_RISKY).next_phase == "tester"
    assert decide("tester", "pass", FEATURE_RISKY).next_phase == "security"


def test_no_sizing_is_the_full_chain():
    assert routing.decide_routing({"history": []}, "reviewer", "approved", NO_GATES).next_phase == "tester"
    assert routing.decide_routing({"history": []}, "designer", "ready-for-builder",
                                  NO_GATES).next_phase == "architect"


def test_changes_requested_still_go_back_to_the_builder_when_not_risky():
    assert decide("reviewer", "changes_requested", FEATURE_SAFE).next_phase == "builder"


# ---------------------------------------------------------------------------
# Start to finish, through the commands
# ---------------------------------------------------------------------------

def start(task_id, config, brief):
    task_dir = settings.TASKS_DIR / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text(brief)
    commands.cmd_start(task_id, auto=False, config=config)


def where(task_id, config):
    status = read_api.status_payload(task_id, config)
    return status["task"]["status"], status["task"]["current_phase"]


def test_a_fix_starts_at_the_builder_and_ends_at_the_reviewer(config, capsys):
    start("typo", config, "---\nsize: fix\nrisky: no\ndeploy: no\n---\nFixed means the typo is gone.\n")
    out = capsys.readouterr().out
    assert "Phase: builder" in out and "Size: fix; risky: no" in out
    assert where("typo", config) == ("in_progress", "builder")
    finish_step("typo", config, "ready-for-review")
    commands.cmd_next("typo", auto=False, config=config)
    # This repo has no test command, so Relay ran no tests: the owner decides.
    gate = read_api.gate_check_payload("typo", config)["gate"]
    assert (gate["kind"], gate["next_phase"]) == ("approval", "reviewer")
    commands.cmd_override("typo", "reviewer", config)
    assert where("typo", config) == ("in_progress", "reviewer")
    finish_step("typo", config, "approved")
    commands.cmd_next("typo", auto=False, config=config)
    assert where("typo", config)[0] == "completed"


def test_a_feature_with_no_structural_change_skips_the_architect(config):
    start("feat", config, "---\nsize: feature\nrisky: no\ndeploy: no\n---\n# Feature\n")
    assert where("feat", config) == ("in_progress", "designer")
    finish_step("feat", config, "ready-for-builder")
    commands.cmd_next("feat", auto=False, config=config)
    assert where("feat", config) == ("in_progress", "builder")


def test_a_brief_with_no_sizing_starts_at_the_designer_and_says_why(config, capsys):
    start("old", config, "# Brief\n")
    out = capsys.readouterr().out
    assert "Phase: designer" in out and "Size: project; risky: yes (the brief does not give" in out


def test_the_sizing_read_at_start_is_the_one_routed_on_even_if_the_brief_changes(config):
    start("pinned", config, "---\nsize: feature\nrisky: yes\ndeploy: no\n---\n# F\n")
    (settings.TASKS_DIR / "pinned" / "brief.md").write_text("---\nsize: fix\nrisky: no\ndeploy: no\n---\n")
    got = sizing.read_sizing("pinned", config)
    assert (got.size, got.risky) == ("feature", True)


def test_a_brief_that_deploys_sets_the_deploy_intent(config):
    start("ships", config, "---\nsize: fix\nrisky: no\ndeploy: yes\n---\n# F\n")
    assert read_api.status_payload("ships", config)["task"]["deploy_intent"] is True


def test_work_that_is_not_risky_stops_when_relay_ran_no_tests():
    not_run = {"outcome": "not_run", "reason": "no profile"}
    decision = routing.decide_routing({"history": []}, "builder", "ready-for-review", NO_GATES,
                                      sizing=FEATURE_SAFE, test_run=not_run)
    assert (decision.outcome, decision.next_phase) == (routing.ROUTE_OWNER_GATE, "reviewer")
    assert "Relay ran no tests" in decision.reason
    risky = routing.decide_routing({"history": []}, "builder", "ready-for-review", NO_GATES,
                                   sizing=FEATURE_RISKY, test_run=not_run)
    assert risky.outcome == routing.ROUTE_ADVANCE
