"""The chain is sized to the task. A brief's
frontmatter gives `size` (fix, feature, project) and `risky` (yes, no);
a brief that gives neither runs the full chain Relay always ran."""

import pytest

import commands
import db
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
    ("---\nsize: Fix\nrisky: No\ndeploy: NO\n---\n", ("fix", False, None, False)),
    ("---\nsize: ' FEATURE '\nrisky: yes\ndeploy: no\n---\n", ("feature", True, None, False)),
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


def test_warnings_on_work_that_is_not_risky_go_to_the_arbiter():
    """Since the Arbiter, a warnings verdict goes to it whatever the sizing.
    A warnings gate left standing from before still stops for the owner,
    where accepting closes the task."""
    decision = decide("reviewer", "approved_with_warnings", FEATURE_SAFE)
    assert (decision.outcome, decision.next_phase) == (routing.ROUTE_ADVANCE, "arbiter")
    standing = routing.decide_routing({"history": []}, "reviewer", "approved_with_warnings",
                                      NO_GATES, sizing=FEATURE_SAFE, already_routed=True)
    assert (standing.outcome, standing.next_phase) == (routing.ROUTE_OWNER_GATE, "done")


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
    assert commands.cmd_start(task_id, auto=False, config=config) == 0


def where(task_id, config):
    status = read_api.status_payload(task_id, config)
    return status["task"]["status"], status["task"]["current_phase"]


def test_a_fix_starts_at_the_builder_and_ends_at_the_reviewer(config, capsys):
    start("typo", config, "---\nsize: fix\nrisky: no\ndeploy: no\n---\nFixed means the typo is gone.\n")
    out = capsys.readouterr().out
    assert "Phase: builder" in out and "Size: fix; risky: no" in out
    assert where("typo", config) == ("in_progress", "builder")
    finish_step("typo", config, "ready-for-review")
    assert commands.cmd_next("typo", auto=False, config=config) == 0
    # This repo has no test command, so Relay ran no tests: the owner decides.
    gate = read_api.gate_check_payload("typo", config)["gate"]
    assert (gate["kind"], gate["next_phase"]) == ("approval", "reviewer")
    assert commands.cmd_override("typo", "reviewer", config) == 0
    assert where("typo", config) == ("in_progress", "reviewer")
    finish_step("typo", config, "approved")
    assert commands.cmd_next("typo", auto=False, config=config) == 0
    assert where("typo", config)[0] == "completed"


def test_a_feature_with_no_structural_change_skips_the_architect(config):
    start("feat", config, "---\nsize: feature\nrisky: no\ndeploy: no\n---\n# Feature\n")
    assert where("feat", config) == ("in_progress", "designer")
    finish_step("feat", config, "ready-for-builder")
    assert commands.cmd_next("feat", auto=False, config=config) == 0
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


# ---------------------------------------------------------------------------
# A raise in a report's frontmatter
# ---------------------------------------------------------------------------

FIX_SAFE = sizing.Sizing("fix", False, None, False)
FEATURE_SAFE_NO_DEPLOY = sizing.Sizing("feature", False, None, False)


def judged(frontmatter, before=FIX_SAFE, required=False, by="reviewer"):
    return sizing.judgment_in(frontmatter, before, by=by, step=5, answer_required=required)


def test_a_raise_raises_each_field():
    got = judged({"sizing": "raise", "risky": True, "size": "project", "sizing_reason": "Touches login."})
    assert (got.after.size, got.after.risky) == ("project", True)
    assert got.changed and got.raised_to_project and got.reason == "Touches login."
    assert (got.refused, got.lowered, got.unanswered) == ((), (), None)
    only_risky = judged({"sizing": "raise", "risky": "yes", "sizing_reason": "r"})
    assert (only_risky.after.size, only_risky.after.risky) == ("fix", True)


def test_a_lowering_is_refused_and_named():
    got = judged({"sizing": "raise", "risky": "no", "size": "fix", "sizing_reason": "small"},
                 before=sizing.Sizing("feature", True, None, False))
    assert not got.changed and got.asked_to_lower
    assert got.lowered == ("size: fix (lower than feature)", "risky: no (lower than yes)")
    assert got.after == got.before


@pytest.mark.parametrize("field, value, refusal", [
    ("size", "huge", "size 'huge' (not fix, feature or project)"),
    ("size", ["project"], "size \"['project']\" (not fix, feature or project)"),
    ("risky", "maybe", "risky 'maybe' (not yes or no)"),
    ("risky", 1, "risky '1' (not yes or no)"),
])
def test_a_value_outside_the_words_is_refused(field, value, refusal):
    got = judged({"sizing": "raise", field: value, "sizing_reason": "r"})
    assert got.refused == (refusal,) and not got.changed and not got.asked_to_lower


@pytest.mark.parametrize("value", ["Project", "PROJECT", " project "])
def test_a_raised_size_is_read_in_any_case_like_the_answer_and_risky(value):
    got = judged({"sizing": "raise", "size": value, "sizing_reason": "r"})
    assert got.after.size == "project" and got.refused == ()


def test_a_lower_size_in_another_case_is_still_a_lowering():
    got = judged({"sizing": "raise", "size": "FIX", "sizing_reason": "r"}, before=FEATURE_SAFE_NO_DEPLOY)
    assert not got.changed and got.lowered == ("size: fix (lower than feature)",)


def test_an_answer_outside_the_words_is_refused_and_raises_nothing():
    got = judged({"sizing": "lower", "risky": True})
    assert got.answer is None and not got.changed
    assert got.refused == ("sizing 'lower' (not raise or keep)",)


def test_raise_one_lower_the_other_gives_the_raise_only():
    got = judged({"sizing": "raise", "size": "project", "risky": False, "sizing_reason": "r"},
                 before=sizing.Sizing("feature", True, None, False))
    assert (got.after.size, got.after.risky) == ("project", True)
    assert got.lowered == ("risky: no (lower than yes)",)


def test_raising_to_the_same_value_changes_nothing():
    got = judged({"sizing": "raise", "size": "feature", "risky": False, "sizing_reason": "r"},
                 before=FEATURE_SAFE_NO_DEPLOY)
    assert not got.changed and (got.refused, got.lowered) == ((), ())


def test_size_and_risky_are_ignored_without_sizing_raise():
    for frontmatter in ({"risky": True, "size": "project"},
                        {"sizing": "keep", "risky": True, "size": "project", "sizing_reason": "r"}):
        got = judged(frontmatter)
        assert not got.changed and got.after == FIX_SAFE


def test_a_raise_without_a_reason_still_applies():
    got = judged({"sizing": "raise", "risky": True})
    assert got.after.risky and got.reason == sizing.NO_REASON_GIVEN


def test_a_reason_is_one_printable_line_cut_at_the_limit():
    got = judged({"sizing": "raise", "risky": True,
                  "sizing_reason": "line one\nline\x1b[2J two " + "x" * 400})
    assert "\n" not in got.reason and "\x1b" not in got.reason
    assert len(got.reason) == sizing.MOST_REASON_CHARACTERS


def test_deploy_is_never_changed():
    got = judged({"sizing": "raise", "risky": True, "size": "project", "deploy": True},
                 before=sizing.Sizing("fix", False, None, False))
    assert got.after.deploy is False


SIZE_ONLY_RAISE = "the Reviewer's raise leaves risky at no, so it does not answer the risk hint"


@pytest.mark.parametrize("frontmatter, problem", [
    ({}, "the Reviewer did not answer the risk hint"),
    ({"sizing": ["raise"]}, "the Reviewer's answer to the risk hint could not be read"),
    ({"sizing": "keep"}, "the Reviewer kept the sizing against the risk hint without a reason"),
    ({"sizing": "keep", "sizing_reason": "   "},
     "the Reviewer kept the sizing against the risk hint without a reason"),
    ({"sizing": "raise", "risky": "perhaps"}, "the Reviewer's raise names nothing Relay can raise"),
    ({"sizing": "raise"}, "the Reviewer's raise names nothing Relay can raise"),
    ({"sizing": "raise", "size": "feature", "sizing_reason": "Bigger than a fix."}, SIZE_ONLY_RAISE),
    ({"sizing": "raise", "size": "project"}, SIZE_ONLY_RAISE),
    ({"sizing": "raise", "size": "project", "risky": "no", "sizing_reason": "r"}, SIZE_ONLY_RAISE),
    ({"sizing": "raise", "size": "project", "risky": "perhaps", "sizing_reason": "r"}, SIZE_ONLY_RAISE),
])
def test_unanswered_cases(frontmatter, problem):
    assert judged(frontmatter, required=True).unanswered == problem
    assert judged(frontmatter, required=False).unanswered is None


def test_a_usable_answer_is_never_unanswered():
    assert judged({"sizing": "keep", "sizing_reason": "Only a test fixture."}, required=True).unanswered is None
    assert judged({"sizing": "raise", "risky": True}, required=True).unanswered is None
    assert judged({"sizing": "raise", "size": "project", "risky": "yes"}, required=True).unanswered is None


def test_a_report_that_is_not_a_mapping_reads_as_no_answer():
    got = sizing.judgment_in(["not", "a", "mapping"], FIX_SAFE, by="reviewer", step=1,
                             answer_required=True)
    assert got.unanswered == "the Reviewer did not answer the risk hint" and not got.changed


def test_the_owner_may_change_either_way_and_deploy_is_kept():
    lowered = sizing.owner_change(sizing.Sizing("project", True, "full chain", True), "fix", False)
    assert lowered == sizing.Sizing("fix", False, None, True)
    assert sizing.owner_change(FIX_SAFE, None, True) == sizing.Sizing("fix", True, None, False)


# ---------------------------------------------------------------------------
# The files backend reads raises back from the reports
# ---------------------------------------------------------------------------

def files_task(tmp_path, monkeypatch, brief, reports):
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path)
    task_dir = tmp_path / "on-files"
    task_dir.mkdir()
    (task_dir / "brief.md").write_text(brief)
    for name, frontmatter in reports.items():
        (task_dir / name).write_text(f"---\n{frontmatter}---\n\nBody.\n")
    return {"persistence_backend": "files"}


def test_the_files_backend_folds_raises_from_reports(tmp_path, monkeypatch):
    config = files_task(tmp_path, monkeypatch, "---\nsize: fix\nrisky: no\ndeploy: no\n---\n", {
        "01-builder.md": "verdict: ready-for-review\nsizing: raise\nrisky: yes\n",
        "02-reviewer.md": "verdict: approved\nsizing: raise\nrisky: yes\nsizing_reason: login\n",
        "03-arbiter.md": "verdict: move-on\nsizing: raise\nrisky: no\nsize: feature\n",
        "04-reviewer-pass-1.md": "verdict: approved\nsizing: raise\nrisky: no\n",
    })
    got = sizing.read_sizing("on-files", config)
    assert (got.size, got.risky, got.deploy) == ("feature", True, False)
    before_step_3 = sizing.read_sizing_before_step("on-files", config, 3)
    assert (before_step_3.size, before_step_3.risky) == ("fix", True)
    history = sizing.read_sizing_history("on-files", config)
    assert [(entry["trigger"], entry["size"], entry["risky"]) for entry in history] == [
        ({"by": "start", "step": None}, "fix", False),
        ({"by": "reviewer", "step": 2}, "fix", True),
        ({"by": "arbiter", "step": 3}, "feature", True)]
    assert history[1]["reason"] == "login"


def test_the_files_backend_skips_a_report_it_cannot_read(tmp_path, monkeypatch):
    config = files_task(tmp_path, monkeypatch, "---\nsize: fix\nrisky: no\ndeploy: no\n---\n", {
        "02-reviewer.md": "verdict: approved\nsizing: [raise\n",
        "03-reviewer.md": "&anchor verdict: approved\nsizing: raise\nrisky: yes\n",
    })
    (tmp_path / "on-files" / "04-reviewer.md").mkdir()
    got = sizing.read_sizing("on-files", config)
    assert (got.size, got.risky) == ("fix", False)


# ---------------------------------------------------------------------------
# The history in the ledger
# ---------------------------------------------------------------------------

def test_history_reads_start_and_changes(config):
    start("grown", config, "---\nsize: fix\nrisky: no\ndeploy: no\n---\n# F\n")
    conn = settings.get_db_connection()
    db.record_sizing_change(conn, "grown", before={"size": "fix", "risky": False, "deploy": False},
                            after={"size": "fix", "risky": True, "deploy": False}, reason="login",
                            by="reviewer", phase="reviewer", step=2)
    db.record_sizing_change(conn, "grown", before={"size": "fix", "risky": True, "deploy": False},
                            after={"size": "fix", "risky": False, "deploy": False}, reason="test only",
                            by="owner", phase=None, step=2)
    history = sizing.read_sizing_history("grown", config)
    assert [(entry["trigger"], entry["from"], entry["risky"], entry["reason"]) for entry in history] == [
        ({"by": "start", "step": None}, None, False, None),
        ({"by": "reviewer", "step": 2}, {"size": "fix", "risky": False}, True, "login"),
        ({"by": "owner", "step": 2}, {"size": "fix", "risky": True}, False, "test only")]
    assert all(entry["timestamp"] for entry in history)
    assert sizing.read_sizing("grown", config).risky is False


def test_an_unreadable_sized_payload_is_shown_as_unreadable(config):
    start("garbled", config, "---\nsize: fix\nrisky: no\ndeploy: no\n---\n# F\n")
    db.record_event(settings.get_db_connection(), "garbled", db.EVENT_SIZED,
                    {"size": "enormous", "risky": "very", "trigger": {"by": "reviewer", "step": 3}})
    history = sizing.read_sizing_history("garbled", config)
    assert history[-1] == {"unreadable": True}
    got = sizing.read_sizing("garbled", config)
    assert (got.size, got.risky) == ("project", True) and "could not be read" in got.note
