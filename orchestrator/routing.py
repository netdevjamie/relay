"""What happens after a phase finishes: the routing table and the one
decision function (`decide_routing`) that `relay next` and the read API
share. Nothing here has side effects.
"""

import dataclasses
from dataclasses import dataclass, field

import arbiter
import evidence
import sizing as sizing_module
from arbiter import HIGH_VERDICTS, WARNING_VERDICTS  # noqa: F401 (HIGH_VERDICTS is read from here)
from settings import ARBITER, PIPELINE_ORDER
from run_state import check_retry_limit, fix_rounds


# verdict -> (next phase, human gate). next phase None means the workflow
# stops there: done when there is no gate, waiting on the owner when there is.
ROUTING = {
    "designer": {
        "ready-for-architect": ("architect", False),  # brief gate: see after_designer
        # The structure does not change, so no Architect (except on a
        # project: see decide_routing). Same brief gate.
        "ready-for-builder": ("builder", False),
        "needs-human-input": (None, True),
        "recommend-no-build": (None, True),  # the owner decides whether to build
    },
    "architect": {
        "ready-for-builder": ("builder", False),
        "needs-designer": ("designer", False),
        "needs-human-input": (None, True),
    },
    "builder": {
        "ready-for-review": ("reviewer", False),
        "blocked": (None, True),
        "needs-architect": ("architect", False),
    },
    "reviewer": {
        "approved": ("tester", False),
        "approved_with_warnings": ("tester", True),
        "changes_requested": ("builder", False),
        "blocked": (None, True),
    },
    "tester": {
        "pass": ("security", False),
        "pass_with_concerns": ("security", True),
        "fail": ("builder", False),
    },
    "security": {
        "secure": ("ops", False),  # ops only runs for deploy tasks; see decide_routing
        "findings": ("ops", True),
        "critical-findings": ("builder", False),
    },
    "ops": {
        # Prepare mode: nothing ships until the owner approves the deploy.
        "deploy-ready": (None, True),
        "not-ready": (None, True),
        "needs-manual-steps": (None, True),
        # Execute mode.
        "deployed": (None, False),
        "not-executed": (None, True),
        "rolled-back": (None, True),
        "deploy-failed": (None, True),
    },
}
# The legacy `pentest` phase has no routes: a task whose latest step is one
# (none is open) stops for the owner as an unknown verdict. Old ledgers and
# run.yaml files that name it still load and read: settings.LEGACY_PHASES.


def determine_next(phase: str, verdict: str, config: dict) -> tuple[str | None, bool, str]:
    """
    Returns (next_phase, needs_human, reason).
    next_phase is None if workflow is done or blocked.
    """
    routes = ROUTING.get(phase, {})
    if verdict not in routes:
        return None, True, f"Unknown verdict '{verdict}' from {phase}. Human must decide."

    next_phase, is_gate = routes[verdict]

    # Config-driven gates: the owner reviews the brief and/or the plan.
    gates = config["human_gates"]
    if phase == "designer" and next_phase in ("architect", "builder") and gates.get("after_designer", True):
        is_gate = True
    if phase == "architect" and next_phase == "builder" and gates.get("after_architect"):
        is_gate = True

    if next_phase is None and not is_gate:
        return None, False, "Workflow complete."

    approval = ", requires human approval to proceed" if is_gate else ""
    return next_phase, is_gate, f"{phase} verdict: {verdict}{approval}"


# ---------------------------------------------------------------------------
# The routing decision
# ---------------------------------------------------------------------------

# Python: Returning None (rather than guessing) lets the caller treat
#   'unknown' differently from yes or no, which is how we fail closed.
def _deploy_flag(value) -> bool | None:
    """The Architect's `deploy` frontmatter as a bool. YAML already turns
    yes/no into booleans; quoted strings are accepted too. Anything else is
    ignored (None) rather than guessed."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "true"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("no", "false"):
        return False
    return None


# Python: list.index(x) gives x's position, so comparing positions tells us
#   whether routing moved backward through the pipeline.
def _is_loopback(phase: str, next_phase: str | None) -> bool:
    """True when routing goes back to an earlier pipeline phase."""
    if phase not in PIPELINE_ORDER or next_phase not in PIPELINE_ORDER:
        return False
    return PIPELINE_ORDER.index(next_phase) < PIPELINE_ORDER.index(phase)


def _tester_ran_since_last_build(state: dict) -> bool:
    """True if the task's recorded history has, after its most recent
    Builder step, a passing Tester step or an Arbiter move-on past a Tester
    high: the Tester ran on this build and its report was weighed, so
    sending the task back to it would only call the Arbiter again. Uses the
    ledger's history, not files on disk: migrated tasks may be missing
    artifacts by design."""
    steps = [entry for entry in state.get("history", []) if isinstance(entry, dict)]
    last_build = max((index for index, entry in enumerate(steps) if entry.get("phase") == "builder"),
                     default=-1)
    since_build = steps[last_build + 1:]
    steps_since_build = {entry.get("step") for entry in since_build}
    return any((entry.get("phase") == "tester" and entry.get("verdict") in ("pass", "pass_with_concerns"))
               or _moved_on_past_a_tester_high(state, entry, steps_since_build)
               for entry in since_build)


def _moved_on_past_a_tester_high(state: dict, entry: dict, steps_since_build: set) -> bool:
    """True when `entry` is an Arbiter move-on whose trigger is a Tester
    report among `steps_since_build`."""
    if entry.get("phase") != ARBITER or entry.get("verdict") != arbiter.MOVE_ON:
        return False
    step = entry.get("step")
    trigger = arbiter.trigger_of(state.get("history"), step) if isinstance(step, int) else None
    return trigger is not None and trigger.phase == "tester" and trigger.step in steps_since_build


# What `relay next` does once the latest step has a verdict.
ROUTE_ADVANCE = "advance"                          # run next_phase
ROUTE_COMPLETE = "complete"                        # the workflow is done
ROUTE_COMPLETE_SKIPPING_OPS = "complete_skipping_ops"  # done: not a deploy task
ROUTE_OWNER_GATE = "owner_gate"                    # stop for the owner
ROUTE_RETRY_LIMIT = "retry_limit"                  # stop: too many automatic rounds in a row

# Why the owner is needed (RoutingDecision.gate_kind).
GATE_APPROVAL = "approval"                      # approve moving to next_phase
GATE_DEPLOY_APPROVAL = "deploy_approval"        # Ops says deploy-ready: ship it, then close
GATE_DEPLOY_FLAG_MISSING = "deploy_flag_missing"  # the plan lacks deploy: yes|no
GATE_OWNER_DECISION = "owner_decision"          # no suggested phase: the owner decides
GATE_RETRY_LIMIT = "retry_limit"                # too many automatic rounds in a row into next_phase
GATE_BLOCKED = "blocked"                        # the task is paused
# The gate of a tripwire Relay once had in the Arbiter's place. Nothing
# stops there; it is kept so a task an older Relay stopped there still reads.
GATE_FIX_ROUNDS = "fix_rounds"
GATE_ARBITER_ESCALATION = "arbiter_escalation"    # the Arbiter asks the owner to decide
GATE_ARBITER_NEEDS_OWNER = "arbiter_needs_owner"  # the Arbiter's ruling needs the owner's yes
GATE_ARBITER_UNREADABLE = "arbiter_unreadable"    # Relay could not act on the Arbiter's report
GATE_RISK_UNANSWERED = "risk_unanswered"          # the Reviewer did not answer the sizing hint

# HIGH_VERDICTS and WARNING_VERDICTS live in arbiter.py and are imported
# above by name, so modules that read them from here keep working.

# Phases whose findings the Builder fixes. Work from any other phase is sent
# back to that phase itself: a brief to the Designer, a plan to the Architect.
# The Arbiter rules only on a Reviewer's, Tester's or Security's report, so
# its step goes back to the Builder too.
PHASES_FIXED_BY_BUILDER = ("reviewer", "tester", "security", ARBITER)


# The gates at which the owner may send work back: where there is finished
# work to accept or reject, or a limit on rounds stopped the task.
SEND_BACK_GATE_KINDS = (GATE_APPROVAL, GATE_OWNER_DECISION, GATE_RETRY_LIMIT, GATE_FIX_ROUNDS,
                        GATE_ARBITER_ESCALATION, GATE_ARBITER_NEEDS_OWNER, GATE_ARBITER_UNREADABLE,
                        GATE_RISK_UNANSWERED)

# Where a move-on that needs the owner would close the task.
_CLOSING_OUTCOMES = (ROUTE_COMPLETE, ROUTE_COMPLETE_SKIPPING_OPS)


# Verdicts that say "this is ready". Before routing on one, `relay next`
# runs the repo's tests itself (see evidence.py) and believes that run over
# the agent's own account.
VERDICTS_RELAY_TESTS = {
    ("builder", "ready-for-review"),
    ("tester", "pass"),
    ("tester", "pass_with_concerns"),
}


def send_back_target(phase: str) -> str:
    """Which phase reworks a finished step when the owner sends it back."""
    return "builder" if phase in PHASES_FIXED_BY_BUILDER else phase


# frozen=True: a decision can't be edited after it is made.
@dataclass(frozen=True)
class RoutingDecision:
    """The answer to "what happens next for this task?", with no side effects.

    `relay next` acts on it; the read API (`relay gate-check`) reports it.
    Both use decide_routing() so they can never disagree."""
    outcome: str
    next_phase: str | None
    reason: str
    gate_kind: str | None = None
    counts_as_retry: bool = False
    redirected_to_tester: bool = False
    send_back_recommended: bool = False
    # Set at a fix_rounds gate left standing from before the Arbiter: the
    # gate says how many fix rounds have run (fix_round_summary).
    lists_fix_rounds: bool = False
    # Set when the next phase is the Arbiter: the situation it is called in.
    arbiter_situation: str | None = None
    # Set when the step routed on is the Arbiter's: the ruling it gave (or
    # the problem that kept Relay from acting on it) and the report it
    # ruled on.
    ruling: "arbiter.Ruling | None" = None
    trigger: "arbiter.Trigger | None" = None
    arbiter_problem: str | None = None
    # Why a move-on stops for the owner, in Relay's words.
    needs_owner_because: tuple[str, ...] = ()
    # The choice the Arbiter recommends at this gate: "accept", or one of an
    # escalation's choices by name. Nothing else is ever recommended.
    recommends: str | None = None
    # Set where the retry limit stopped a route the Arbiter would have
    # taken: the gate says how many review loops ran on this plan.
    lists_review_loops: bool = False
    # Set when a report's raise of the size to project sends the task to
    # the Architect, which has not run on it yet.
    architect_for_raise: bool = False


# ---------------------------------------------------------------------------
# decide_routing: an ordered list of rules
# ---------------------------------------------------------------------------

# Python: a dataclass without frozen=True can be changed after it is made:
#   each rule below edits the one draft in turn.
@dataclass
class _Draft:
    """What the rules know about one routing decision while they run: the
    inputs, and the answer so far. Only decide_routing makes one."""
    state: dict
    phase: str
    verdict: str
    config: dict
    planned_deploy: bool | None
    force_phase: str | None
    test_run: dict | None
    test_command_confirmed: bool
    sizing: "sizing_module.Sizing"
    arbiter_report: dict | None
    latest_task_test_run: dict | None
    loop_counts: "arbiter.LoopCounts"
    already_routed: bool
    judgment: "sizing_module.Judgment | None" = None
    next_phase: str | None = None
    needs_human: bool = False
    reason_parts: list[str] = field(default_factory=list)
    routed_back: bool = False
    missing_deploy_flag: bool = False
    redirected_to_tester: bool = False
    risk_unanswered: bool = False

    @property
    def reason(self) -> str:
        return "; ".join(self.reason_parts)


def _override(draft: _Draft) -> RoutingDecision | None:
    """The owner's manual override goes where it says, past every gate."""
    if draft.force_phase:
        return RoutingDecision(ROUTE_ADVANCE, draft.force_phase,
                               f"Manual override to {draft.force_phase}")
    return None


def _architect_for_raise(draft: _Draft) -> RoutingDecision | None:
    """A Reviewer's or Arbiter's report that raised the size to project
    sends the task to the Architect when none has run on it, whatever the
    verdict, except one that is the owner's (blocked, or an Arbiter's
    escalation). It adds a step; it is not a failure loop. Where an
    Architect has run, the raise is only recorded."""
    judgment = draft.judgment
    if (judgment is None or not judgment.raised_to_project
            or draft.verdict in ("blocked", arbiter.ESCALATE)
            or any(isinstance(entry, dict) and entry.get("phase") == "architect"
                   for entry in draft.state.get("history") or [])):
        return None
    role = sizing_module.ROLE_NAMES.get(judgment.by, judgment.by)
    return RoutingDecision(ROUTE_ADVANCE, "architect",
                           f"{draft.phase} verdict: {draft.verdict}; the {role} raised the size to "
                           "project and no Architect has run, so the Architect plans next",
                           architect_for_raise=True)


def _routing_table(draft: _Draft) -> None:
    """Start from the routing table's answer (determine_next)."""
    next_phase, needs_human, reason = determine_next(draft.phase, draft.verdict, draft.config)
    draft.next_phase, draft.needs_human, draft.reason_parts = next_phase, needs_human, [reason]


def _sizing(draft: _Draft) -> None:
    """Apply the task's recorded size and risk: a project always gets the Architect,
    and work that is not risky goes from the Reviewer where Security's
    approval would have gone."""
    if draft.phase == "designer" and draft.next_phase == "builder" and draft.sizing.size == "project":
        draft.next_phase = "architect"
        draft.reason_parts.append("a project always gets the Architect")
    if draft.phase == "reviewer" and draft.next_phase == "tester" and not draft.sizing.risky:
        draft.next_phase = "ops"
        draft.reason_parts.append("the task is sized not risky, so no Tester or Security")


def _loopback(draft: _Draft) -> None:
    """Note whether the table's answer goes back to an earlier phase. Noted
    here, before the old-order redirect (security -> tester), so that
    redirect never counts as a failure loop."""
    draft.routed_back = _is_loopback(draft.phase, draft.next_phase)


def _tests_beat_words(draft: _Draft) -> None:
    """The agent says ready, Relay's test run says otherwise: the Builder
    fixes it. This is an automatic loop back like any other, even from the
    Builder's own step, so it counts toward the retry limit."""
    if _tests_block(draft.phase, draft.verdict, draft.test_run):
        draft.next_phase, draft.needs_human, draft.routed_back = "builder", False, True
        draft.reason_parts = [f"{draft.phase} verdict: {draft.verdict}, but Relay's own test run "
                              f"did not pass ({draft.test_run['outcome']}); back to the builder"]


def _new_test_command(draft: _Draft) -> None:
    """A passing run the owner must confirm stops for the owner until
    they have accepted what it flags: a test command or report path the base branch
    doesn't have, harness files the branch changed, or fewer tests run
    (evidence.confirmation_reason). The branch under test must not decide
    by itself what "the tests" are."""
    test_run = draft.test_run
    if (test_run and (draft.phase, draft.verdict) in VERDICTS_RELAY_TESTS
            and not _tests_block(draft.phase, draft.verdict, test_run)
            and not draft.test_command_confirmed):
        reason = evidence.confirmation_reason(test_run)
        if reason:
            draft.needs_human = True
            draft.reason_parts.append(reason)


def _no_tests(draft: _Draft) -> None:
    """Work that is not risky gets no Tester: Relay's own run at the
    Builder's step is its only test evidence. When Relay could run nothing
    (no test command in the repo's profile), the owner decides before it
    goes on."""
    test_run = draft.test_run
    if (not draft.sizing.risky and draft.phase == "builder" and draft.verdict == "ready-for-review"
            and test_run and test_run.get("outcome") == evidence.OUTCOME_NOT_RUN):
        draft.needs_human = True
        draft.reason_parts.append(f"Relay ran no tests ({test_run.get('reason') or 'no test command'})"
                                  " and no Tester will, so the owner decides")


def _missing_deploy_flag(draft: _Draft) -> None:
    """An approved plan must say whether it deploys; a missing or unreadable
    flag stops for the owner instead of silently skipping Ops."""
    if draft.phase == "architect" and draft.next_phase == "builder" and draft.planned_deploy is None:
        draft.missing_deploy_flag = True
        draft.needs_human = True
        draft.reason_parts.append("the plan has no valid deploy: yes|no, so the owner decides")


def _risk_unanswered(draft: _Draft) -> None:
    """The Reviewer had to answer a sizing hint that matched on a task not
    risky, and did not (or its answer cannot be used): the owner decides,
    before the Arbiter would. Never an automatic raise: the hint is not a
    rule."""
    judgment = draft.judgment
    if judgment is None or not judgment.unanswered:
        return
    draft.needs_human = True
    draft.reason_parts.append(f"{judgment.unanswered}, so the owner decides")
    if draft.verdict != "blocked":
        draft.risk_unanswered = True


def _arbiter_trigger(draft: _Draft) -> RoutingDecision | None:
    """A report that calls the Arbiter goes to it: a high once the review
    loops on this plan reach arbiter.REVIEW_LOOPS_BEFORE_THE_ARBITER, and
    every warning verdict. A high below that goes back to the Builder. A
    failing test run goes to the Builder with no Arbiter (tests beat words).

    The retry limit's stop comes first: where it would stop the route back
    to the Builder, the stop is the limit's and no default is recommended.

    A gate left standing from before the Arbiter (the verdict was recorded
    by an earlier `relay next` and nothing followed) reads as it did then,
    except that a fix_rounds gate recommends nothing."""
    situation = arbiter.situation_of(draft.phase, draft.verdict)
    if (situation is None or _tests_block(draft.phase, draft.verdict, draft.test_run)
            or draft.risk_unanswered):
        return None
    high = situation == arbiter.SITUATION_LOOP
    loops = draft.loop_counts.review_loops_since_replan
    threshold = arbiter.REVIEW_LOOPS_BEFORE_THE_ARBITER
    standing_high = draft.already_routed and fix_rounds(draft.state) >= threshold
    if high and (loops >= threshold or standing_high) and not check_retry_limit(
            draft.state, "builder", draft.config):
        return RoutingDecision(
            ROUTE_RETRY_LIMIT, "builder",
            f"{draft.phase} verdict: {draft.verdict}; {_review_loops_in_words(loops)} on this plan, "
            "and the limit on automatic rounds is reached, so the owner decides",
            gate_kind=GATE_RETRY_LIMIT, lists_review_loops=True)
    if draft.already_routed:
        if high and standing_high:
            return RoutingDecision(
                ROUTE_OWNER_GATE, "builder",
                f"{draft.phase} verdict: {draft.verdict}; {fix_rounds(draft.state)} fix rounds have "
                "run and this report still holds a high, so the owner decides",
                gate_kind=GATE_FIX_ROUNDS, lists_fix_rounds=True)
        return None
    if high and loops < threshold:
        return None
    reason = f"{draft.phase} verdict: {draft.verdict}"
    if high:
        reason += f"; {_review_loops_in_words(loops)} on this plan"
    return RoutingDecision(ROUTE_ADVANCE, ARBITER, f"{reason}, so the Arbiter decides",
                           arbiter_situation=situation)


def _review_loops_in_words(loops: int) -> str:
    return "1 review loop" if loops == 1 else f"{loops} review loops"


def _arbiter_ruling(draft: _Draft) -> RoutingDecision | None:
    """An Arbiter step routes on its ruling, read from its report's
    frontmatter (or the ruling Relay recorded from it, once it has acted
    on it), and never on the routing table. A ruling Relay cannot act
    on stops for the owner and never moves the task."""
    if draft.phase != ARBITER:
        return None
    trigger = arbiter.trigger_of_latest(draft.state.get("history"))
    if trigger is None:
        return _unreadable("Relay found no report the Arbiter was ruling on", None)
    ruling, problem = arbiter.read_ruling(draft.arbiter_report or {}, trigger.situation)
    if problem:
        return _unreadable(problem, trigger)
    if ruling.decision == arbiter.TARGETED_ROUND:
        return _targeted_round(draft, ruling, trigger)
    if ruling.decision == arbiter.REPLAN:
        return _replan(draft, ruling, trigger)
    if ruling.decision == arbiter.ESCALATE:
        return _escalate(draft, ruling, trigger)
    return _move_on(draft, ruling, trigger)


def _escalate(draft: _Draft, ruling: "arbiter.Ruling", trigger: "arbiter.Trigger") -> RoutingDecision:
    """The owner's gate with the Arbiter's question. Relay's own warnings
    go with it, in Relay's words, as they would with a move-on: Security
    still holds a high, the latest test run did not pass, a choice would
    close the task or reach Ops. A choice that closes or reaches Ops skips
    Security, so where one is offered a Builder fix does not lift
    Security's high: it stands until Security reports again. While a
    warning stands, a choice that closes or reaches Ops is never the
    recommended one. Where the trigger's passing run awaits the owner's
    confirmation (a changed test command, changed or unchecked harness
    files, fewer tests run) and a choice moves past the trigger with no
    new Builder step, Relay's reason is given too, and no such choice is
    the recommended one."""
    warnings = _standing_warnings(draft)
    reaching = [choice for choice in ruling.choices if _closes_or_reaches_ops(choice)]
    if reaching and _security_high_unchecked_since_a_fix(draft.state):
        warnings = (UNCHECKED_SECURITY_HIGH,) + warnings
    past_the_trigger = [choice for choice in ruling.choices
                        if choice.action == arbiter.DOES_MOVE_TO
                        and moves_past(choice.phase, trigger.phase)]
    harness = unconfirmed_run_reason(draft.test_run, draft.test_command_confirmed)
    harness_warning = (harness,) if harness and past_the_trigger else ()
    because = warnings + harness_warning + tuple(
        f"the Arbiter's choice {choice.name} would "
        f"{'close the task' if choice.phase == 'done' else 'reach Ops'}" for choice in reaching)
    recommends = ruling.recommended
    if warnings and recommends in {choice.name for choice in reaching}:
        recommends = None
    if harness_warning and recommends in {choice.name for choice in past_the_trigger}:
        recommends = None
    return RoutingDecision(ROUTE_OWNER_GATE, None, "the Arbiter asks the owner to decide",
                           gate_kind=GATE_ARBITER_ESCALATION, ruling=ruling, trigger=trigger,
                           needs_owner_because=because, recommends=recommends)


def _closes_or_reaches_ops(choice: "arbiter.Choice") -> bool:
    return choice.action == arbiter.DOES_MOVE_TO and choice.phase in ("done", "ops")


def moves_past(phase: str, trigger_phase: str) -> bool:
    """True when moving to `phase` goes on past the report the Arbiter
    ruled on (done counts as the last phase), so no Builder step comes
    before the next phase is judged on that report's run."""
    order = arbiter.MOVE_TO_PHASES
    return phase in order and trigger_phase in order and order.index(phase) > order.index(trigger_phase)


def unconfirmed_run_reason(test_run: dict | None, confirmed: bool) -> str | None:
    """Relay's reason a passing run awaits the owner's confirmation
    (evidence.confirmation_reason), or None when there is no passing run
    or the owner has already accepted what it flags."""
    if not test_run or confirmed or test_run.get("outcome") != evidence.OUTCOME_PASSED:
        return None
    return evidence.confirmation_reason(test_run)


def _unreadable(problem: str, trigger: "arbiter.Trigger | None") -> RoutingDecision:
    return RoutingDecision(ROUTE_OWNER_GATE, None,
                           f"Relay could not act on the Arbiter's decision: {problem}",
                           gate_kind=GATE_ARBITER_UNREADABLE, trigger=trigger,
                           arbiter_problem=problem)


def _targeted_round(draft: _Draft, ruling: "arbiter.Ruling",
                    trigger: "arbiter.Trigger") -> RoutingDecision:
    """One more Builder round, counted as an automatic round."""
    if not check_retry_limit(draft.state, "builder", draft.config):
        return RoutingDecision(ROUTE_RETRY_LIMIT, "builder",
                               "the Arbiter ruled a targeted round, and the limit on automatic "
                               "rounds is reached, so the owner decides",
                               gate_kind=GATE_RETRY_LIMIT, ruling=ruling, trigger=trigger)
    return RoutingDecision(ROUTE_ADVANCE, "builder",
                           f"the Arbiter ruled a targeted round ({trigger.phase} report at step "
                           f"{trigger.step}); back to the builder",
                           counts_as_retry=True, ruling=ruling, trigger=trigger)


def _replan(draft: _Draft, ruling: "arbiter.Ruling", trigger: "arbiter.Trigger") -> RoutingDecision:
    """Back to the Architect or the Designer; a second re-plan on one task
    is the owner's."""
    step = arbiter.latest_step(draft.state.get("history"))
    earlier = [entry for entry in draft.state.get("history") or []
               if isinstance(entry, dict) and entry.get("phase") == ARBITER
               and entry.get("verdict") == arbiter.REPLAN and entry.get("step") != step]
    target = ruling.replan_to
    if earlier:
        return RoutingDecision(ROUTE_OWNER_GATE, target,
                               "the Arbiter ruled a re-plan a second time on this task, so the "
                               "owner decides", gate_kind=GATE_ARBITER_NEEDS_OWNER, ruling=ruling,
                               trigger=trigger,
                               needs_owner_because=("the Arbiter ruled a re-plan a second time "
                                                    "on this task",))
    if not check_retry_limit(draft.state, target, draft.config):
        return RoutingDecision(ROUTE_RETRY_LIMIT, target,
                               f"the Arbiter ruled a re-plan to the {target}, and the limit on "
                               "automatic rounds is reached, so the owner decides",
                               gate_kind=GATE_RETRY_LIMIT, ruling=ruling, trigger=trigger)
    return RoutingDecision(ROUTE_ADVANCE, target, f"the Arbiter ruled a re-plan; to the {target}",
                           counts_as_retry=True, ruling=ruling, trigger=trigger)


def _move_on(draft: _Draft, ruling: "arbiter.Ruling", trigger: "arbiter.Trigger") -> RoutingDecision:
    """Where a clean pass of the trigger report would have gone, under the
    task's sizing and with the trigger's own test run, unless the move needs
    the owner: Security still holds a high, Relay's latest test run did not
    pass, or the move would close the task or reach Ops."""
    clean = decide_routing(draft.state, trigger.phase, arbiter.CLEAN_PASS[trigger.phase],
                           draft.config, test_run=draft.test_run,
                           test_command_confirmed=draft.test_command_confirmed, sizing=draft.sizing)
    closes = clean.outcome in _CLOSING_OUTCOMES or (
        clean.outcome == ROUTE_OWNER_GATE and clean.next_phase == "done")
    because = _move_on_needs_owner(draft, clean, closes)
    if because:
        return RoutingDecision(ROUTE_OWNER_GATE, "done" if closes else clean.next_phase,
                               "the Arbiter ruled to move on, and " + "; ".join(because)
                               + ", so the owner decides",
                               gate_kind=GATE_ARBITER_NEEDS_OWNER, ruling=ruling, trigger=trigger,
                               needs_owner_because=because, recommends="accept",
                               redirected_to_tester=clean.redirected_to_tester)
    if clean.outcome == ROUTE_OWNER_GATE:
        return dataclasses.replace(clean, ruling=ruling, trigger=trigger, recommends="accept")
    return dataclasses.replace(clean, ruling=ruling, trigger=trigger, counts_as_retry=False,
                               reason=f"the Arbiter ruled to move on; {clean.reason}")


def _move_on_needs_owner(draft: _Draft, clean: RoutingDecision, closes: bool) -> tuple[str, ...]:
    """Why a move-on is the owner's to approve, in Relay's words."""
    because = list(_standing_warnings(draft))
    if closes:
        because.append("moving on would close the task")
    elif clean.next_phase == "ops":
        because.append("moving on would reach Ops")
    return tuple(because)


def _standing_warnings(draft: _Draft) -> tuple[str, ...]:
    """What still stands against the work whatever the Arbiter ruled, in
    Relay's words: a Security high since the latest build, and Relay's
    latest test run not passing."""
    warnings = []
    if _security_high_since_the_latest_build(draft.state):
        warnings.append("Security's latest report still holds a high")
    run = draft.latest_task_test_run
    if run and run.get("outcome") != evidence.OUTCOME_PASSED:
        warnings.append("Relay's latest test run did not pass")
    return tuple(warnings)


UNCHECKED_SECURITY_HIGH = ("Security's latest report holds a high, and Security has not run "
                           "since the fix")


def _security_high_unchecked_since_a_fix(state: dict) -> bool:
    """Security's latest report is a high, a Builder step came after it,
    and no Security report since. A Security step that left no verdict is
    not a report."""
    entries = [entry for entry in state.get("history") or [] if isinstance(entry, dict)]
    reports = [index for index, entry in enumerate(entries)
               if entry.get("phase") == "security" and entry.get("verdict")]
    if not reports or entries[reports[-1]].get("verdict") != "critical-findings":
        return False
    return any(entry.get("phase") == "builder" for entry in entries[reports[-1] + 1:])


def _security_high_since_the_latest_build(state: dict) -> bool:
    entries = [entry for entry in state.get("history") or [] if isinstance(entry, dict)]
    last_build = max((index for index, entry in enumerate(entries) if entry.get("phase") == "builder"),
                     default=-1)
    return any(entry.get("phase") == "security" and entry.get("verdict") == "critical-findings"
               for entry in entries[last_build + 1:])


def _complete(draft: _Draft) -> RoutingDecision | None:
    """Nowhere left to go and nothing for the owner to decide: done."""
    if draft.next_phase is None and not draft.needs_human:
        return RoutingDecision(ROUTE_COMPLETE, None, draft.reason)
    return None


def _legacy_tester_redirect(draft: _Draft) -> None:
    """Tasks started under the old order ran Security before the Tester. If
    one finishes Security without a passing Tester step since its last
    build, run the Tester now rather than skipping it.

    Delete this rule once no open task in `relay list` has a Security step
    before its first Tester step in its history (`relay status <task>`)."""
    if (draft.phase == "security" and draft.next_phase == "ops"
            and not _tester_ran_since_last_build(draft.state)):
        draft.redirected_to_tester = True
        draft.next_phase = "tester"


def _ops_only_for_deploy(draft: _Draft) -> RoutingDecision | None:
    """Ops runs only for deploy tasks. A clean pass has nothing left to
    decide, so the task completes. A verdict that needs the owner still
    gates: what the owner approves is closing the task, not moving to Ops."""
    if draft.next_phase == "ops" and not draft.state.get("deploy_intent"):
        if not draft.needs_human:
            return RoutingDecision(ROUTE_COMPLETE_SKIPPING_OPS, None, draft.reason)
        draft.next_phase = "done"
    return None


def _owner_gate_kind(draft: _Draft) -> RoutingDecision | None:
    """Stop for the owner when a rule above needs the owner, saying why."""
    if not draft.needs_human:
        return None
    if draft.missing_deploy_flag:
        gate_kind = GATE_DEPLOY_FLAG_MISSING
    elif draft.risk_unanswered:
        gate_kind = GATE_RISK_UNANSWERED
    elif draft.next_phase:
        gate_kind = GATE_APPROVAL
    elif draft.phase == "ops" and draft.verdict == "deploy-ready":
        gate_kind = GATE_DEPLOY_APPROVAL
    else:
        gate_kind = GATE_OWNER_DECISION
    return RoutingDecision(ROUTE_OWNER_GATE, draft.next_phase, draft.reason, gate_kind=gate_kind,
                           redirected_to_tester=draft.redirected_to_tester,
                           send_back_recommended=(draft.phase, draft.verdict) in WARNING_VERDICTS)


def _retry_limit(draft: _Draft) -> RoutingDecision | None:
    """Nobody chose this loop back, so it counts toward the retry limit,
    which stops the task once reached. (One the owner chooses, by send-back
    or override, never comes through here.)"""
    if draft.routed_back and not check_retry_limit(draft.state, draft.next_phase, draft.config):
        return RoutingDecision(ROUTE_RETRY_LIMIT, draft.next_phase, draft.reason,
                               gate_kind=GATE_RETRY_LIMIT)
    return None


# The rules decide_routing applies, in this order. A rule may change the
# draft or end the decision by returning one; the first decision returned
# wins, and when none is, the task advances to the draft's next phase.
# The order is the behaviour, so change it only on purpose. These are the
# orderings that matter:
# - the override comes first: the owner's choice skips everything;
# - a raise of the size to project comes next, before the Arbiter's ruling:
#   a raise in a report applies whatever its verdict or ruling says (except
#   a decision that is the owner's), and the Architect has not run;
# - the Arbiter's ruling comes before the table: an Arbiter step routes on
#   its ruling, never on the routing table;
# - sizing changes the table's answer before the loop back is noted from it;
# - tests-beat-words comes before the Arbiter's trigger (a failing run goes
#   to the Builder with no Arbiter) and before the new-test-command rule (a
#   failing run never gates); it replaces the reason and the route, so the
#   rules after it add to its reason, not the table's;
# - the Arbiter's trigger comes before the completion, the redirect and the
#   owner gate, so a report that calls the Arbiter goes to it whatever the
#   table or another reason for a gate says; the Arbiter's move-on asks
#   those again;
# - the rules that set needs_human come before the completion and the owner
#   gate, the only two that read it; their order among themselves, and the
#   Arbiter's trigger's place among them, does not matter, except that the
#   Reviewer's missing answer to the sizing hint comes before the Arbiter's
#   trigger: the owner decides that first, and the choices include send-back;
# - the redirect runs before ops-only-for-deploy, so a redirected task goes
#   to the Tester and is not completed for skipping Ops;
# - the owner gate comes before the retry limit: a loop back that needs the
#   owner is the owner's gate, not the limit's.
_RULES = (
    _override,
    _architect_for_raise,
    _arbiter_ruling,
    _routing_table,
    _sizing,
    _loopback,
    _tests_beat_words,
    _new_test_command,
    _no_tests,
    _missing_deploy_flag,
    _risk_unanswered,
    _arbiter_trigger,
    _complete,
    _legacy_tester_redirect,
    _ops_only_for_deploy,
    _owner_gate_kind,
    _retry_limit,
)


def decide_routing(state: dict, phase: str, verdict: str, config: dict, *,
                   planned_deploy: bool | None = None,
                   force_phase: str | None = None,
                   test_run: dict | None = None,
                   test_command_confirmed: bool = False,
                   sizing: "sizing_module.Sizing | None" = None,
                   arbiter_report: dict | None = None,
                   latest_task_test_run: dict | None = None,
                   loop_counts: "arbiter.LoopCounts | None" = None,
                   already_routed: bool = False,
                   judgment: "sizing_module.Judgment | None" = None) -> RoutingDecision:
    """Decide what follows `phase` finishing with `verdict`. Reads only.

    `planned_deploy` is the Architect plan's deploy flag (None when missing or
    unreadable). It only matters while the latest step is the Architect's,
    where a missing flag stops for the owner.

    `test_run` is Relay's own recorded test run for this step, if there is
    one (for an Arbiter step, the run at the report it ruled on). A run that
    failed outranks a verdict that says the work is ready. A run that passed
    with a test command the base branch doesn't have stops for the owner
    until they have accepted that command once (`test_command_confirmed`).

    `sizing` is the task's recorded size and risk (sizing.py); None runs
    the full chain. `judgment` is what a Reviewer's or Arbiter's report says
    of the sizing (sizing.Judgment): when given, its `after` sizing governs
    every rule for this report, the Arbiter's move-on included, so a raise
    takes effect at once; a raise to project may send the task to the
    Architect, and a missing answer the Reviewer owed stops for the owner.

    `arbiter_report` is the frontmatter of the Arbiter's report, when the
    step is the Arbiter's: once Relay has recorded what it did with the
    ruling, the recorded ruling in that shape (arbiter_record.recorded_report). `latest_task_test_run` is the task's newest
    recorded test run. `loop_counts` is arbiter.loop_counts for the task;
    None works it out from the history alone. `already_routed` is True when
    an earlier `relay next` recorded this step's verdict and nothing
    followed: a gate left standing.

    The rules are in _RULES, applied in order; each one's docstring says
    what it does."""
    if loop_counts is None:
        loop_counts = arbiter.loop_counts(state.get("history"), [], VERDICTS_RELAY_TESTS)
    draft = _Draft(state=state, phase=phase, verdict=verdict, config=config,
                   planned_deploy=planned_deploy, force_phase=force_phase, test_run=test_run,
                   test_command_confirmed=test_command_confirmed,
                   sizing=(judgment.after if judgment is not None
                           else sizing or sizing_module.FULL_CHAIN),
                   arbiter_report=arbiter_report,
                   latest_task_test_run=latest_task_test_run, loop_counts=loop_counts,
                   already_routed=already_routed, judgment=judgment)
    for rule in _RULES:
        decision = rule(draft)
        if decision is not None:
            return decision
    return RoutingDecision(ROUTE_ADVANCE, draft.next_phase, draft.reason,
                           counts_as_retry=draft.routed_back,
                           redirected_to_tester=draft.redirected_to_tester)


def _tests_block(phase: str, verdict: str, test_run: dict | None) -> bool:
    """True when Relay's recorded test run contradicts a "ready" verdict."""
    return bool(test_run and (phase, verdict) in VERDICTS_RELAY_TESTS
                and test_run.get("outcome") in evidence.FAILING_OUTCOMES)


def fix_round_summary(state: dict) -> dict:
    """What a fix_rounds gate left standing from before the Arbiter says:
    {"count": fix rounds, "lines": one sentence saying how many have run}.
    A count, not a reading of the reports."""
    count = fix_rounds(state)
    rounds = "1 fix round has" if count == 1 else f"{count} fix rounds have"
    return {"count": count,
            "lines": [f"{rounds} run and the latest report still asks for fixes."]}
