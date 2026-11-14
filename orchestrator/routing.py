"""What happens after a phase finishes: the routing table and the one
decision function (`decide_routing`) that `relay next` and the read API
share. Nothing here has side effects.
"""

from dataclasses import dataclass

import evidence
import sizing as sizing_module
from settings import PIPELINE_ORDER
from run_state import check_retry_limit, fix_rounds


# verdict -> (next phase, human gate). next phase None means the workflow
# stops there: done when there is no gate, waiting on the owner when there is.
ROUTING = {
    "designer": {
        "ready-for-architect": ("architect", False),  # brief gate: see after_designer
        # The structure does not change, so no Architect
        # (except on a project: see decide_routing). Same brief gate.
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
    "pentest": {  # legacy
        "secure": ("ops", False),
        "findings": ("ops", True),
        "vulnerable": ("builder", False),
    },
}


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

    reason = f"{phase} verdict: {verdict}"
    if is_gate:
        reason += ", requires human approval to proceed"

    return next_phase, is_gate, reason


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
    """True if the task's recorded history has a passing Tester step after
    its most recent Builder step. Uses the ledger's history, not files on disk:
    migrated tasks may be missing artifacts by design."""
    steps = [entry for entry in state.get("history", []) if isinstance(entry, dict)]
    # Python: (... for ... if ...) in parentheses is a generator: it produces values
    #   one at a time without building a list. dict.get('key') returns None
    #   instead of raising when the key is missing.
    last_build = max((index for index, entry in enumerate(steps) if entry.get("phase") == "builder"),
                     default=-1)
    # Python: any() is True if at least one item is truthy; it stops at the first hit.
    #   steps[last_build + 1:] is a slice: everything after the last build.
    return any(entry.get("phase") == "tester" and entry.get("verdict") in ("pass", "pass_with_concerns")
               for entry in steps[last_build + 1:])


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
GATE_FIX_ROUNDS = "fix_rounds"                  # fix rounds are still finding highs

# Once a task has had this many fix rounds, a report that still holds a high
# stops for the owner instead of going back to the Builder, and so does every
# round after it. Read here only: after two rounds the owner decides.
FIX_ROUNDS_BEFORE_THE_OWNER_DECIDES = 2

# The verdict each rating role gives when its highest rating is high: the
# reports the tripwire counts as still asking for fixes.
HIGH_VERDICTS = {("reviewer", "changes_requested"), ("tester", "fail"),
                 ("security", "critical-findings")}

# Verdicts that pass with something still wrong. At these gates the
# recommended answer is to send the work back for fixes, not to accept it.
WARNING_VERDICTS = {
    ("reviewer", "approved_with_warnings"),
    ("tester", "pass_with_concerns"),
    ("security", "findings"),
    ("pentest", "findings"),
}

# Phases whose findings the Builder fixes. Work from any other phase is sent
# back to that phase itself: a brief to the Designer, a plan to the Architect.
PHASES_FIXED_BY_BUILDER = ("reviewer", "tester", "security", "pentest")


# The gates at which the owner may send work back: where there is finished
# work to accept or reject, or a limit on rounds stopped the task.
SEND_BACK_GATE_KINDS = (GATE_APPROVAL, GATE_OWNER_DECISION, GATE_RETRY_LIMIT, GATE_FIX_ROUNDS)


# Verdicts that say "this is ready". Before routing on one, `relay next`
# runs the repo's tests itself (see evidence.py) and believes that run over
# the agent's own account.
VERDICTS_RELAY_TESTS = {
    ("builder", "ready-for-review"),
    ("tester", "pass"),
    ("tester", "pass_with_concerns"),
}


_TEST_COMMAND_AWAITS_OWNER = ("this task changed the test command or report path, so the owner "
                              "confirms it before the passing run is believed")


def send_back_target(phase: str) -> str:
    """Which phase reworks a finished step when the owner sends it back."""
    return "builder" if phase in PHASES_FIXED_BY_BUILDER else phase


# Python: @dataclass writes __init__ and friends from the field list.
#   frozen=True makes instances read-only, so a decision can't be edited
#   after it is made.
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
    # Set when the tripwire stopped the task: the gate says how many fix
    # rounds have run (fix_round_summary).
    lists_fix_rounds: bool = False


def decide_routing(state: dict, phase: str, verdict: str, config: dict, *,
                   planned_deploy: bool | None = None,
                   force_phase: str | None = None,
                   test_run: dict | None = None,
                   test_command_confirmed: bool = False,
                   sizing: "sizing_module.Sizing | None" = None) -> RoutingDecision:
    """Decide what follows `phase` finishing with `verdict`. Reads only.

    `planned_deploy` is the Architect plan's deploy flag (None when missing or
    unreadable). It only matters while the latest step is the Architect's,
    where a missing flag stops for the owner.

    `test_run` is Relay's own recorded test run for this step, if there is
    one. A run that failed outranks a verdict that says the work is ready.
    A run that passed with a test command the base branch doesn't have stops
    for the owner until they have accepted that command once
    (`test_command_confirmed`): the branch under test must not get to decide
    by itself what "the tests" are.

    A report that holds a high after FIX_ROUNDS_BEFORE_THE_OWNER_DECIDES fix
    rounds stops for the owner instead of going back to the Builder (see
    _fix_round_stop).

    `sizing` is what the task's brief says of its size and risk
    (sizing.py); None runs the full chain. A project always gets the
    Architect, even when the Designer says the structure does not change.
    Work that is not risky ends its checks at the Reviewer: an approval
    goes where Security's would have gone (Ops on a deploy task, else done)."""
    if force_phase:
        return RoutingDecision(ROUTE_ADVANCE, force_phase, f"Manual override to {force_phase}")

    sizing = sizing or sizing_module.FULL_CHAIN
    next_phase, needs_human, reason = determine_next(phase, verdict, config)
    if phase == "designer" and next_phase == "builder" and sizing.size == "project":
        next_phase = "architect"
        reason += "; a project always gets the Architect"
    if phase == "reviewer" and next_phase == "tester" and not sizing.risky:
        next_phase = "ops"
        reason += "; the brief says the work is not risky, so no Tester or Security"
    # Decided here, from the routing table's answer, so the old-order redirect
    # below (security -> tester) never counts as a failure loop.
    routed_back = _is_loopback(phase, next_phase)

    # The agent says ready, Relay's test run says otherwise: the Builder
    # fixes it. This is an automatic loop back like any other, even from the
    # Builder's own step, so it counts toward the retry limit.
    if _tests_block(phase, verdict, test_run):
        next_phase, needs_human, routed_back = "builder", False, True
        reason = (f"{phase} verdict: {verdict}, but Relay's own test run did not pass "
                  f"({test_run['outcome']}); back to the builder")
    elif (test_run and (phase, verdict) in VERDICTS_RELAY_TESTS
          and test_run.get("command_differs_from_base") and not test_command_confirmed):
        needs_human = True
        reason += f"; {_TEST_COMMAND_AWAITS_OWNER}"

    # Work that is not risky gets no Tester: Relay's own run at the Builder's
    # step is its only test evidence. When Relay could run nothing (no test
    # command in the repo's profile), the owner decides before it goes on.
    if (not sizing.risky and phase == "builder" and verdict == "ready-for-review"
            and test_run and test_run.get("outcome") == evidence.OUTCOME_NOT_RUN):
        needs_human = True
        reason += ("; Relay ran no tests (" + str(test_run.get("reason") or "no test command")
                   + ") and no Tester will, so the owner decides")

    # An approved plan must say whether it deploys; a missing or unreadable
    # flag stops for the owner instead of silently skipping Ops.
    missing_deploy_flag = (phase == "architect" and next_phase == "builder"
                           and planned_deploy is None)
    if missing_deploy_flag:
        needs_human = True
        reason += "; the plan has no valid deploy: yes|no, so the owner decides"

    if (phase, verdict) in HIGH_VERDICTS and (
            fix_rounds(state) >= FIX_ROUNDS_BEFORE_THE_OWNER_DECIDES):
        return _fix_round_stop(state, phase, verdict, next_phase, config)

    if next_phase is None and not needs_human:
        return RoutingDecision(ROUTE_COMPLETE, None, reason)

    # Tasks started by older versions of Relay ran Security before the Tester. If
    # one finishes Security without a Tester step yet, run the Tester now
    # rather than skipping it.
    redirected_to_tester = (phase in ("security", "pentest") and next_phase == "ops"
                            and not _tester_ran_since_last_build(state))
    if redirected_to_tester:
        next_phase = "tester"

    # Ops runs only for deploy tasks. A clean pass has nothing left to decide,
    # so the task completes. A verdict that needs the owner still gates: what
    # the owner approves is closing the task, not moving to Ops.
    if next_phase == "ops" and not state.get("deploy_intent"):
        if not needs_human:
            return RoutingDecision(ROUTE_COMPLETE_SKIPPING_OPS, None, reason)
        next_phase = "done"

    if needs_human:
        if missing_deploy_flag:
            gate_kind = GATE_DEPLOY_FLAG_MISSING
        elif next_phase:
            gate_kind = GATE_APPROVAL
        elif phase == "ops" and verdict == "deploy-ready":
            gate_kind = GATE_DEPLOY_APPROVAL
        else:
            gate_kind = GATE_OWNER_DECISION
        return RoutingDecision(ROUTE_OWNER_GATE, next_phase, reason, gate_kind=gate_kind,
                               redirected_to_tester=redirected_to_tester,
                               send_back_recommended=(phase, verdict) in WARNING_VERDICTS)

    # Nobody chose this loop back, so it counts toward the retry limit. (One
    # the owner chooses, by send-back or override, never comes through here.)
    if routed_back and not check_retry_limit(state, next_phase, config):
        return RoutingDecision(ROUTE_RETRY_LIMIT, next_phase, reason, gate_kind=GATE_RETRY_LIMIT)

    return RoutingDecision(ROUTE_ADVANCE, next_phase, reason, counts_as_retry=routed_back,
                           redirected_to_tester=redirected_to_tester)


def _fix_round_stop(state: dict, phase: str, verdict: str, next_phase: str,
                    config: dict) -> RoutingDecision:
    """The tripwire's stop: the owner decides whether another round runs,
    and is told how many fix rounds have run. It never ends the task.
    When the retry limit would stop the same route, there is one stop: the
    limit's, which keeps a blocked task reading as stopped at the limit, with
    the list and a recommended send-back."""
    reason = (f"{phase} verdict: {verdict}; {fix_rounds(state)} fix rounds have run and this "
              "report still holds a high")
    if not check_retry_limit(state, next_phase, config):
        return RoutingDecision(ROUTE_RETRY_LIMIT, next_phase,
                               f"{reason}, and the limit on automatic rounds is reached, so the "
                               "owner decides", gate_kind=GATE_RETRY_LIMIT,
                               send_back_recommended=True, lists_fix_rounds=True)
    return RoutingDecision(ROUTE_OWNER_GATE, next_phase, f"{reason}, so the owner decides",
                           gate_kind=GATE_FIX_ROUNDS, send_back_recommended=True,
                           lists_fix_rounds=True)


def _tests_block(phase: str, verdict: str, test_run: dict | None) -> bool:
    """True when Relay's recorded test run contradicts a "ready" verdict."""
    return bool(test_run and (phase, verdict) in VERDICTS_RELAY_TESTS
                and test_run.get("outcome") in evidence.FAILING_OUTCOMES)


def fix_round_summary(state: dict) -> dict:
    """What the tripwire's stop says: {"count": fix rounds, "lines": one
    sentence saying how many have run}. A count, not a reading of the
    reports: the latest verdict is what asks for fixes."""
    count = fix_rounds(state)
    rounds = "1 fix round has" if count == 1 else f"{count} fix rounds have"
    return {"count": count,
            "lines": [f"{rounds} run and the latest report still asks for fixes."]}
