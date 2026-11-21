"""The Arbiter's plumbing (arbiter.py): reading a ruling from a report's
frontmatter, refusing every ruling Relay cannot act on exactly, finding the
report a ruling answers, and counting Builder steps by cause.

Pure: every test hands the module plain dicts.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arbiter
from routing import VERDICTS_RELAY_TESTS

LOOP, WARNING = arbiter.SITUATION_LOOP, arbiter.SITUATION_WARNING
ESCALATION = {"verdict": "escalate", "reason": "The owner must choose.",
              "question": "Accept the threat model?",
              "choices": [{"says": "Accept it.", "does": "move-to tester"},
                          {"says": "Send it back.", "does": "send-back"},
                          {"says": "Stop.", "does": "abort"},
                          {"says": "Just note it.", "does": "note"}],
              "recommended": 2}


# ---------------------------------------------------------------------------
# Reading a ruling
# ---------------------------------------------------------------------------

def test_read_ruling_accepts_each_decision_with_its_parts():
    move_on, problem = arbiter.read_ruling(
        {"verdict": "move-on", "reason": " Cosmetic. ", "follow_ups": ["Reword it."],
         "known_flaws": ["Slow on big files."]}, WARNING)
    assert problem is None
    assert (move_on.decision, move_on.reason, move_on.follow_ups, move_on.known_flaws) == (
        "move-on", "Cosmetic.", ("Reword it.",), ("Slow on big files.",))

    targeted, _ = arbiter.read_ruling({"verdict": "targeted-round", "reason": "Real.",
                                       "fix": ["Bound the parse."], "do_not_reopen": ["Checks."]},
                                      LOOP)
    assert (targeted.fix, targeted.do_not_reopen) == (("Bound the parse.",), ("Checks.",))

    replan, _ = arbiter.read_ruling({"verdict": "re-plan", "reason": "Plan.",
                                     "replan_to": "designer"}, LOOP)
    assert (replan.decision, replan.replan_to) == ("re-plan", "designer")

    escalation, _ = arbiter.read_ruling(ESCALATION, WARNING)
    assert escalation.question == "Accept the threat model?"
    assert escalation.choices == (
        arbiter.Choice("arbiter-1", "Accept it.", "move-to", "tester"),
        arbiter.Choice("arbiter-2", "Send it back.", "send-back"),
        arbiter.Choice("arbiter-3", "Stop.", "abort"),
        arbiter.Choice("arbiter-4", "Just note it.", "note"))
    assert escalation.recommended == "arbiter-2"


def escalation_with(**changed) -> dict:
    return {**ESCALATION, **changed}


ESCALATION_PROBLEM = ("an escalation needs a question, two to five choices that each say what "
                      "they do, and a recommended choice")


@pytest.mark.parametrize("frontmatter, situation, problem", [
    ({}, LOOP, "its report has no frontmatter Relay can read"),
    ("not a mapping", LOOP, "its report has no frontmatter Relay can read"),
    ({"verdict": "move on", "reason": "x"}, LOOP,
     "its verdict 'move on' is not one of move-on, targeted-round, re-plan, escalate"),
    ({"verdict": "ship-it\x1b[31m" + "x" * 100, "reason": "x"}, LOOP,
     f"its verdict {'ship-it[31m' + 'x' * 49!r} is not one of move-on, targeted-round, re-plan, "
     "escalate"),
    ({"verdict": ["move-on"], "reason": "x"}, LOOP,
     "its verdict \"['move-on']\" is not one of move-on, targeted-round, re-plan, escalate"),
    ({"verdict": "re-plan", "reason": "x", "replan_to": "architect"}, WARNING,
     "re-plan is not open at a warning gate"),
    ({"verdict": "move-on"}, LOOP, "it gives no one-line reason"),
    ({"verdict": "move-on", "reason": "   "}, LOOP, "it gives no one-line reason"),
    ({"verdict": "move-on", "reason": 7}, LOOP, "it gives no one-line reason"),
    ({"verdict": "move-on", "reason": "x" * 401}, LOOP, "it gives no one-line reason"),
    ({"verdict": "re-plan", "reason": "x", "replan_to": "builder"}, LOOP,
     "a re-plan must name replan_to: architect or designer"),
    ({"verdict": "re-plan", "reason": "x"}, LOOP,
     "a re-plan must name replan_to: architect or designer"),
    ({"verdict": "targeted-round", "reason": "x"}, LOOP, "a targeted round must list what to fix"),
    ({"verdict": "targeted-round", "reason": "x", "fix": []}, LOOP,
     "a targeted round must list what to fix"),
    ({"verdict": "targeted-round", "reason": "x", "fix": ""}, LOOP,
     "its fix is not a list of short lines"),
    ({"verdict": "move-on", "reason": "x", "follow_ups": [{"a": 1}]}, LOOP,
     "its follow_ups is not a list of short lines"),
    ({"verdict": "move-on", "reason": "x", "known_flaws": ["x" * 601]}, LOOP,
     "its known_flaws is not a list of short lines"),
    ({"verdict": "move-on", "reason": "x", "follow_ups": ["x"] * 13}, LOOP,
     "its follow_ups is not a list of short lines"),
    ({"verdict": "targeted-round", "reason": "x", "fix": ["a"], "do_not_reopen": [""]}, LOOP,
     "its do_not_reopen is not a list of short lines"),
    (escalation_with(question=None), LOOP, ESCALATION_PROBLEM),
    (escalation_with(question="x" * 401), LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices=[{"says": "Only one.", "does": "abort"}]), LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices=[{"says": "x", "does": "abort"}] * 6), LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices="abort, send-back"), LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices=[{"says": "Ship.", "does": "deploy"}, {"says": "x", "does": "abort"}]),
     LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices=[{"says": "x", "does": "move-to ops-now"},
                              {"says": "x", "does": "abort"}]), LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices=[{"says": "x", "does": "move-to arbiter"},
                              {"says": "x", "does": "abort"}]), LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices=[{"does": "abort"}, {"says": "x", "does": "abort"}]), LOOP,
     ESCALATION_PROBLEM),
    (escalation_with(choices=[{"says": "x" * 201, "does": "abort"},
                              {"says": "x", "does": "abort"}]), LOOP, ESCALATION_PROBLEM),
    (escalation_with(choices=["abort", "send-back"]), LOOP, ESCALATION_PROBLEM),
    (escalation_with(recommended=0), LOOP, ESCALATION_PROBLEM),
    (escalation_with(recommended=5), LOOP, ESCALATION_PROBLEM),
    (escalation_with(recommended=True), LOOP, ESCALATION_PROBLEM),
    (escalation_with(recommended="2"), LOOP, ESCALATION_PROBLEM),
    ({**ESCALATION, "recommended": None}, LOOP, ESCALATION_PROBLEM),
], ids=lambda value: str(value)[:40] if not isinstance(value, dict) else None)
def test_read_ruling_refuses_each_unreadable_case(frontmatter, situation, problem):
    assert arbiter.read_ruling(frontmatter, situation) == (None, problem)


def test_a_valid_ruling_in_an_unknown_situation_is_refused():
    """A situation Relay does not know opens no call."""
    ruling, problem = arbiter.read_ruling({"verdict": "move-on", "reason": "x"}, "elsewhere")
    assert ruling is None and problem == "move-on is not open at a elsewhere gate"


# ---------------------------------------------------------------------------
# The report a ruling answers
# ---------------------------------------------------------------------------

def test_trigger_of_finds_the_report_before_the_arbiter_step_and_skips_an_earlier_arbiter_step():
    history = [{"phase": "builder", "step": 3, "verdict": "ready-for-review"},
               {"phase": "reviewer", "step": 4, "verdict": "approved_with_warnings"},
               {"phase": "arbiter", "step": 5, "verdict": None},
               {"phase": "arbiter", "step": 6},
               "not a step", {"phase": "reviewer", "step": "7"}]
    assert arbiter.trigger_of(history, 6) == arbiter.Trigger(4, "reviewer", "approved_with_warnings",
                                                             WARNING)
    assert arbiter.trigger_of(history, 5) == arbiter.trigger_of(history, 6)
    loop = history[:2] + [{"phase": "security", "step": 5, "verdict": "critical-findings"},
                          {"phase": "arbiter", "step": 6}]
    assert arbiter.trigger_of(loop, 6) == arbiter.Trigger(5, "security", "critical-findings", LOOP)
    assert arbiter.trigger_of(history, 4) is None
    assert arbiter.trigger_of([], 3) is None


# ---------------------------------------------------------------------------
# What the count counts
# ---------------------------------------------------------------------------

def steps_of(*phases_and_verdicts) -> list[dict]:
    return [{"phase": phase, "step": number, "verdict": verdict}
            for number, (phase, verdict) in enumerate(phases_and_verdicts, 1)]


PLANNED = [("designer", "ready-for-architect"), ("architect", "ready-for-builder"),
           ("builder", "ready-for-review")]


@pytest.mark.parametrize("before, runs, cause, role, started_by, counted", [
    ([("reviewer", "changes_requested")], [], "review_loop", "reviewer", "relay", 1),
    ([("tester", "fail")], [], "review_loop", "tester", "relay", 1),
    ([("reviewer", "approved_with_warnings")], [], "review_loop", "reviewer", "owner", 1),
    ([("reviewer", "approved_with_warnings"), ("arbiter", "targeted-round")], [],
     "review_loop", "reviewer", "arbiter", 1),
    ([("security", "critical-findings"), ("arbiter", "escalate")], [],
     "review_loop", "security", "owner", 1),
    ([("tester", "pass")], [{"step": 4, "outcome": "failed"}], "failed_test_run", None, None, 0),
    ([("tester", "pass_with_concerns")], [{"step": 4, "outcome": "timed_out"}],
     "failed_test_run", None, None, 0),
    ([], [{"step": 3, "outcome": "failed"}], "failed_test_run", None, None, 0),
    ([("reviewer", "approved"), ("architect", "ready-for-builder")], [], "after_replan", None, None, 0),
    ([("builder", "needs-architect"), ("architect", "needs-designer"),
      ("designer", "ready-for-builder")], [], "after_replan", None, None, 0),
    ([("reviewer", "approved"), ("tester", "pass"), ("security", "secure"), ("ops", "not-ready")], [],
     "owner_elsewhere", None, None, 0),
    ([("builder", "blocked")], [], "owner_elsewhere", None, None, 0),
], ids=["reviewer high", "tester high", "owner send-back at a warning", "arbiter targeted round",
        "send-back at an escalation", "failing run at a tester pass", "timed out at concerns",
        "failing run at the build", "re-plan by the architect", "re-plan by the designer",
        "override from ops", "override after a blocked build"])
def test_each_kind_of_builder_step_is_counted_or_not(before, runs, cause, role, started_by, counted):
    history = steps_of(*PLANNED, *before, ("builder", None))
    counts = arbiter.loop_counts(history, runs, VERDICTS_RELAY_TESTS)
    assert counts.steps[0] == arbiter.BuilderStep(3, "first_build")
    assert counts.steps[-1] == arbiter.BuilderStep(len(history), cause, role, started_by)
    assert counts.review_loops_since_replan == counted


# A Builder step that ends needs-architect after a Reviewer's report is itself
# a review loop (it followed the report), so those walks hold one more.
@pytest.mark.parametrize("replan, loops_on_the_task", [
    ([("arbiter", "re-plan"), ("architect", "ready-for-builder")], 4),
    ([("builder", "needs-architect"), ("architect", "ready-for-builder")], 5),
    ([("builder", "needs-architect"), ("architect", "needs-designer"),
      ("designer", "ready-for-builder")], 5),
    ([("designer", "ready-for-architect"), ("architect", "ready-for-builder")], 4),
], ids=["an Arbiter ruling", "a Builder's needs-architect", "an Architect's needs-designer",
        "an override to the Designer"])
def test_the_count_starts_again_after_a_re_plan(replan, loops_on_the_task):
    loops = [("reviewer", "changes_requested"), ("builder", "ready-for-review")] * 3
    history = steps_of(*PLANNED, *loops, ("reviewer", "changes_requested"), *replan,
                       ("builder", "ready-for-review"), ("reviewer", "changes_requested"),
                       ("builder", None))
    counts = arbiter.loop_counts(history, [], VERDICTS_RELAY_TESTS)
    replan_step = max(entry["step"] for entry in history if entry["phase"] in ("architect", "designer"))
    assert counts.replan_step == replan_step
    assert counts.review_loops_since_replan == 1
    assert counts.by_cause(False)["review_loop"] == loops_on_the_task


def test_no_build_yet_counts_nothing():
    counts = arbiter.loop_counts(steps_of(*PLANNED[:2]), [], VERDICTS_RELAY_TESTS)
    assert counts == arbiter.LoopCounts()
    assert counts.by_cause(True)["total"] == 0


def test_the_breakdown_by_cause_on_a_task_with_every_cause():
    history = steps_of(
        *PLANNED,                                                         # 3 first build
        ("reviewer", "changes_requested"), ("builder", "ready-for-review"),   # 5 loop, relay
        ("tester", "pass"), ("builder", "ready-for-review"),                  # 7 failed run
        ("tester", "fail"), ("builder", "ready-for-review"),                  # 9 loop, tester
        ("security", "findings"), ("arbiter", "targeted-round"),
        ("builder", "ready-for-review"),                                      # 12 loop, arbiter
        ("arbiter", "re-plan"), ("architect", "ready-for-builder"),
        ("builder", "ready-for-review"),                                      # 15 after re-plan
        ("reviewer", "approved_with_warnings"), ("builder", "ready-for-review"),  # 17 loop, owner
        ("ops", "not-ready"), ("builder", "ready-for-review"))                # 19 owner elsewhere
    counts = arbiter.loop_counts(history, [{"step": 6, "outcome": "failed"}], VERDICTS_RELAY_TESTS)
    assert counts.replan_step == 14
    assert counts.by_cause(False) == {
        "total": 8, "first_build": 1, "review_loop": 4, "failed_test_run": 1, "after_replan": 1,
        "owner_elsewhere": 1,
        "review_loop_by_role": {"reviewer": 2, "tester": 1, "security": 1},
        "review_loop_by_starter": {"relay": 2, "arbiter": 1, "owner": 1}}
    assert counts.by_cause(True) == {
        "total": 3, "first_build": 0, "review_loop": 1, "failed_test_run": 0, "after_replan": 1,
        "owner_elsewhere": 1,
        "review_loop_by_role": {"reviewer": 1, "tester": 0, "security": 0},
        "review_loop_by_starter": {"relay": 0, "arbiter": 0, "owner": 1}}
    assert counts.review_loops_since_replan == 1
