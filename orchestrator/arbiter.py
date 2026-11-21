"""The Arbiter's rules that are plumbing, not judgment: which report called
it, what its report must say for Relay to act on it, and how many review
loops have run on the current plan.

The Arbiter (agents/arbiter.md) reads a task's loop with fresh eyes and
decides what happens next. Relay never reads its prose: it reads the
decision from the report's frontmatter (read_ruling) and refuses anything
it cannot act on exactly, so an unreadable or out-of-place ruling stops for
the owner instead of moving the task. Routing (routing.py) acts on what
this module reads.

This module is pure: it reads dicts and returns values.
"""

from dataclasses import dataclass

import evidence
from printable import printable
from settings import PIPELINE_ORDER

# The four calls, as the report's verdict names them (settings.VALID_VERDICTS).
MOVE_ON = "move-on"
TARGETED_ROUND = "targeted-round"
REPLAN = "re-plan"
ESCALATE = "escalate"
DECISIONS = (MOVE_ON, TARGETED_ROUND, REPLAN, ESCALATE)

# Where the Arbiter is called: a loop past the tripwire, or a warning gate.
SITUATION_LOOP = "loop"
SITUATION_WARNING = "warning"
# A re-plan is open only in a loop: a warning gate passed, so the plan held.
CALLS_OPEN = {SITUATION_LOOP: DECISIONS,
              SITUATION_WARNING: (MOVE_ON, TARGETED_ROUND, ESCALATE)}
REPLAN_TARGETS = ("architect", "designer")

# The verdict each rating role gives when it passes clean. A move-on goes
# where this verdict would have gone.
CLEAN_PASS = {"reviewer": "approved", "tester": "pass", "security": "secure"}

# The verdict each rating role gives when its highest rating is high: the
# reports that still ask for fixes.
HIGH_VERDICTS = {("reviewer", "changes_requested"), ("tester", "fail"),
                 ("security", "critical-findings")}

# Verdicts that pass with something still wrong: each one goes to the
# Arbiter, which accepts it, sends it back for a targeted round, or asks the
# owner.
WARNING_VERDICTS = {
    ("reviewer", "approved_with_warnings"),
    ("tester", "pass_with_concerns"),
    ("security", "findings"),
}

# How many review loops run on one plan before the Arbiter is called: the
# owner's number, plumbing, not the Arbiter's judgment. Read as
# arbiter.REVIEW_LOOPS_BEFORE_THE_ARBITER, so a test can replace it.
REVIEW_LOOPS_BEFORE_THE_ARBITER = 2

# Format caps on what the report may hold. Every list and line of it is
# shown on the card, in status and in later prompts, so each is kept to a
# size a person would write. These live here and in Relay's prompt to the
# Arbiter, never in its role file.
MOST_LIST_ITEMS = 12
MOST_ITEM_CHARACTERS = 600
MOST_REASON_CHARACTERS = 400
MOST_CHOICES = 5
MOST_CHOICE_CHARACTERS = 200
# A verdict outside the four is quoted in the problem, cut to this length.
QUOTED_VERDICT_CHARACTERS = 60

LIST_FIELDS = ("fix", "do_not_reopen", "follow_ups", "known_flaws")

# What an escalation's choice may do: one of the owner's existing choices.
DOES_SEND_BACK, DOES_MOVE_TO, DOES_ABORT, DOES_NOTE = "send-back", "move-to", "abort", "note"
MOVE_TO_PHASES = tuple(PIPELINE_ORDER) + ("done",)

ESCALATION_PROBLEM = ("an escalation needs a question, two to five choices that each say what "
                      "they do, and a recommended choice")


# ---------------------------------------------------------------------------
# Which report called the Arbiter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Trigger:
    """The report an Arbiter step rules on, and the situation it was in."""
    step: int
    phase: str
    verdict: str
    situation: str


def situation_of(phase, verdict) -> str | None:
    """The situation a report puts the Arbiter in: a loop for a high, a
    warning gate for a pass with something still wrong, else None."""
    if (phase, verdict) in HIGH_VERDICTS:
        return SITUATION_LOOP
    if (phase, verdict) in WARNING_VERDICTS:
        return SITUATION_WARNING
    return None


def steps_in_order(history) -> list[dict]:
    """The history's steps in step order, leaving out anything that is not
    a step with a whole step number."""
    steps = [entry for entry in history or []
             if isinstance(entry, dict) and isinstance(entry.get("step"), int)
             and not isinstance(entry.get("step"), bool)]
    return sorted(steps, key=lambda entry: entry["step"])


def trigger_of(history, arbiter_step: int) -> Trigger | None:
    """The latest report before `arbiter_step` that calls the Arbiter: a
    high or a warning verdict from a rating role. A second Arbiter step on
    the same report (one run again) finds the same report. None when there
    is none."""
    for entry in reversed(steps_in_order(history)):
        if entry["step"] >= arbiter_step:
            continue
        situation = situation_of(entry.get("phase"), entry.get("verdict"))
        if situation:
            return Trigger(entry["step"], entry["phase"], entry["verdict"], situation)
    return None


def latest_step(history) -> int | None:
    """The history's highest step number. None when it has no step."""
    steps = [entry.get("step") for entry in history or []
             if isinstance(entry, dict) and isinstance(entry.get("step"), int)]
    return max(steps) if steps else None


def trigger_of_latest(history) -> Trigger | None:
    """The report that calls the Arbiter at the history's latest step
    (trigger_of). None when the history has no step or no such report."""
    step = latest_step(history)
    return trigger_of(history, step) if step is not None else None


# ---------------------------------------------------------------------------
# Reading a ruling from the report's frontmatter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    """One of an escalation's choices: `name` (arbiter-1, ...), the
    Arbiter's words, and what Relay does when the owner picks it."""
    name: str
    says: str
    action: str
    phase: str | None = None


@dataclass(frozen=True)
class Ruling:
    """What the Arbiter decided, as its frontmatter gave it, every field
    checked. `recommended` is the name of an escalation's recommended
    choice."""
    decision: str
    reason: str
    replan_to: str | None = None
    fix: tuple[str, ...] = ()
    do_not_reopen: tuple[str, ...] = ()
    follow_ups: tuple[str, ...] = ()
    known_flaws: tuple[str, ...] = ()
    question: str | None = None
    choices: tuple[Choice, ...] = ()
    recommended: str | None = None


class _Unreadable(Exception):
    """A ruling Relay cannot act on, with the problem in Relay's words."""


def _is_short_text(value, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _short_lines(frontmatter: dict, field: str) -> tuple[str, ...]:
    """A list field as a tuple of its lines; () when it is not there."""
    value = frontmatter.get(field)
    if value is None:
        return ()
    if (not isinstance(value, list) or len(value) > MOST_LIST_ITEMS
            or not all(_is_short_text(item, MOST_ITEM_CHARACTERS) for item in value)):
        raise _Unreadable(f"its {field} is not a list of short lines")
    return tuple(item.strip() for item in value)


def _what_it_does(does) -> tuple[str, str | None]:
    """An escalation choice's `does`, as (action, phase): exactly one of
    the owner's existing choices, or _Unreadable."""
    if not isinstance(does, str):
        raise _Unreadable(ESCALATION_PROBLEM)
    if does in (DOES_SEND_BACK, DOES_ABORT, DOES_NOTE):
        return does, None
    action, _, phase = does.partition(" ")
    if action == DOES_MOVE_TO and phase in MOVE_TO_PHASES:
        return DOES_MOVE_TO, phase
    raise _Unreadable(ESCALATION_PROBLEM)


def _escalation(frontmatter: dict) -> tuple[str, tuple[Choice, ...], str]:
    """An escalation's question, choices and recommended choice's name."""
    question, listed = frontmatter.get("question"), frontmatter.get("choices")
    recommended = frontmatter.get("recommended")
    if (not _is_short_text(question, MOST_REASON_CHARACTERS) or not isinstance(listed, list)
            or not 2 <= len(listed) <= MOST_CHOICES):
        raise _Unreadable(ESCALATION_PROBLEM)
    choices = []
    for number, item in enumerate(listed, 1):
        if not isinstance(item, dict) or not _is_short_text(item.get("says"), MOST_CHOICE_CHARACTERS):
            raise _Unreadable(ESCALATION_PROBLEM)
        action, phase = _what_it_does(item.get("does"))
        choices.append(Choice(f"arbiter-{number}", item["says"].strip(), action, phase))
    # Python: bool is a kind of int, so it is ruled out by name.
    if (not isinstance(recommended, int) or isinstance(recommended, bool)
            or not 1 <= recommended <= len(choices)):
        raise _Unreadable(ESCALATION_PROBLEM)
    return question.strip(), tuple(choices), choices[recommended - 1].name


def _checked(frontmatter, situation: str) -> Ruling:
    """The ruling, every check in order; the first that fails raises."""
    if not isinstance(frontmatter, dict) or not frontmatter:
        raise _Unreadable("its report has no frontmatter Relay can read")
    verdict = frontmatter.get("verdict")
    if verdict not in DECISIONS:
        shown = printable(verdict, QUOTED_VERDICT_CHARACTERS).strip()
        raise _Unreadable(f"its verdict {shown!r} is not one of {', '.join(DECISIONS)}")
    if verdict not in CALLS_OPEN.get(situation, ()):
        raise _Unreadable(f"{verdict} is not open at a {situation} gate")
    reason = frontmatter.get("reason")
    if not _is_short_text(reason, MOST_REASON_CHARACTERS):
        raise _Unreadable("it gives no one-line reason")
    lists = {field: _short_lines(frontmatter, field) for field in LIST_FIELDS}
    replan_to = frontmatter.get("replan_to")
    if verdict == REPLAN and replan_to not in REPLAN_TARGETS:
        raise _Unreadable("a re-plan must name replan_to: architect or designer")
    if verdict == TARGETED_ROUND and not lists["fix"]:
        raise _Unreadable("a targeted round must list what to fix")
    question, choices, recommended = None, (), None
    if verdict == ESCALATE:
        question, choices, recommended = _escalation(frontmatter)
    return Ruling(verdict, reason.strip(), replan_to if verdict == REPLAN else None,
                  question=question, choices=choices, recommended=recommended, **lists)


def read_ruling(frontmatter, situation: str) -> tuple[Ruling | None, str | None]:
    """The Arbiter's ruling from its report's frontmatter, as (ruling,
    problem). The problem, in Relay's words, is set when the report cannot
    be acted on exactly; Relay then stops for the owner and never guesses."""
    try:
        return _checked(frontmatter, situation), None
    except _Unreadable as problem:
        return None, str(problem)


# ---------------------------------------------------------------------------
# What the loop count counts
# ---------------------------------------------------------------------------

# Why a Builder step ran.
CAUSE_FIRST_BUILD = "first_build"
CAUSE_REVIEW_LOOP = "review_loop"          # after a Reviewer, Tester or Security report
CAUSE_FAILED_TEST_RUN = "failed_test_run"  # Relay's own run did not pass
CAUSE_AFTER_REPLAN = "after_replan"        # after an Architect or Designer step
CAUSE_OWNER_ELSEWHERE = "owner_elsewhere"  # an owner's override from anywhere else
CAUSES = (CAUSE_FIRST_BUILD, CAUSE_REVIEW_LOOP, CAUSE_FAILED_TEST_RUN, CAUSE_AFTER_REPLAN,
          CAUSE_OWNER_ELSEWHERE)

# Who started a review loop.
STARTED_BY_RELAY, STARTED_BY_ARBITER, STARTED_BY_OWNER = "relay", "arbiter", "owner"

RATING_PHASES = ("reviewer", "tester", "security")
PLANNING_PHASES = ("architect", "designer")


@dataclass(frozen=True)
class BuilderStep:
    """One Builder step and why it ran. `role` and `started_by` are set for
    a review loop: the rating role whose report it answers, and who sent
    it back (read from the step before it)."""
    step: int
    cause: str
    role: str | None = None
    started_by: str | None = None


@dataclass(frozen=True)
class LoopCounts:
    """A task's Builder steps by cause. `replan_step` is the latest
    Architect or Designer step after the first build, if any."""
    steps: tuple[BuilderStep, ...] = ()
    replan_step: int | None = None
    review_loops_since_replan: int = 0

    def _boundary(self) -> int | None:
        """The step after which "since the re-plan" counts: the re-plan,
        or the first build when there has been none."""
        if self.replan_step is not None:
            return self.replan_step
        return self.steps[0].step if self.steps else None

    def by_cause(self, since_replan: bool) -> dict:
        """The Builder steps counted by cause, for the task or since the
        latest re-plan, with the review loops by role and by who started
        them."""
        boundary = self._boundary()
        counted = [builder for builder in self.steps
                   if not since_replan or (boundary is not None and builder.step > boundary)]
        loops = [builder for builder in counted if builder.cause == CAUSE_REVIEW_LOOP]
        counts = {"total": len(counted)}
        counts.update({cause: sum(builder.cause == cause for builder in counted) for cause in CAUSES})
        counts["review_loop_by_role"] = {role: sum(loop.role == role for loop in loops)
                                         for role in RATING_PHASES}
        counts["review_loop_by_starter"] = {
            starter: sum(loop.started_by == starter for loop in loops)
            for starter in (STARTED_BY_RELAY, STARTED_BY_ARBITER, STARTED_BY_OWNER)}
        return counts


def _run_failed_at(test_runs, step: int) -> bool:
    """True when Relay's newest recorded run at `step` did not pass."""
    runs = [run for run in test_runs or [] if isinstance(run, dict) and run.get("step") == step]
    return bool(runs) and runs[-1].get("outcome") in evidence.FAILING_OUTCOMES


def _builder_step(entry: dict, before: dict, history, test_runs,
                  verdicts_relay_tests) -> BuilderStep:
    """Why the Builder step `entry` ran, read from the step `before` it."""
    phase, verdict = before.get("phase"), before.get("verdict")
    if phase in PLANNING_PHASES:
        return BuilderStep(entry["step"], CAUSE_AFTER_REPLAN)
    if phase == "arbiter":
        trigger = trigger_of(history, before["step"])
        started_by = STARTED_BY_ARBITER if verdict == TARGETED_ROUND else STARTED_BY_OWNER
        return BuilderStep(entry["step"], CAUSE_REVIEW_LOOP, trigger.phase if trigger else None,
                           started_by)
    if (phase, verdict) in verdicts_relay_tests and _run_failed_at(test_runs, before["step"]):
        return BuilderStep(entry["step"], CAUSE_FAILED_TEST_RUN)
    if phase in RATING_PHASES:
        started_by = STARTED_BY_RELAY if (phase, verdict) in HIGH_VERDICTS else STARTED_BY_OWNER
        return BuilderStep(entry["step"], CAUSE_REVIEW_LOOP, phase, started_by)
    return BuilderStep(entry["step"], CAUSE_OWNER_ELSEWHERE)


def loop_counts(history, test_runs, verdicts_relay_tests) -> LoopCounts:
    """The task's Builder steps by cause, worked out from its history and
    Relay's recorded test runs (recorded_runs.read_test_runs). The
    count that calls the Arbiter is `review_loops_since_replan`: Builder
    steps that followed a Reviewer, Tester or Security report, whoever sent
    them back, since the latest re-plan. `verdicts_relay_tests` is
    routing.VERDICTS_RELAY_TESTS, passed in so this module stays below
    routing."""
    entries = steps_in_order(history)
    builds = [index for index, entry in enumerate(entries) if entry.get("phase") == "builder"]
    if not builds:
        return LoopCounts()
    first = builds[0]
    replans = [entry["step"] for entry in entries[first + 1:] if entry.get("phase") in PLANNING_PHASES]
    replan_step = replans[-1] if replans else None
    steps = [BuilderStep(entries[first]["step"], CAUSE_FIRST_BUILD)]
    steps += [_builder_step(entries[index], entries[index - 1], entries, test_runs,
                            verdicts_relay_tests)
              for index in builds[1:]]
    boundary = replan_step if replan_step is not None else entries[first]["step"]
    loops = sum(builder.cause == CAUSE_REVIEW_LOOP and builder.step > boundary for builder in steps)
    return LoopCounts(tuple(steps), replan_step, loops)
