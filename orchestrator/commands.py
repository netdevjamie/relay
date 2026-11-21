"""The commands behind the `relay` CLI: start, next, status, list,
override, send-back, note, resize, validate, abort, recover,
register, gate-check, profile, and the phone's waiting, card, show and
decide.
"""

import fcntl
import json
import os
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import arbiter
import arbiter_record
import awake
import base_trust
import board_view
import db
import evidence
import migrate_legacy
import proto
import risk_hint
import risk_record
import settings
import sizing
import agents
import gate_card
import read_api
from settings import (
    ARBITER, FIRST_PHASE, PIPELINE_ORDER, RELAY_HOME, VALID_PHASES, VALID_STATUSES,
    VALID_VERDICTS, _CLI_CMD, _resolve_repo_path, _task_cwd,
    _sqlite_enabled, dump_yaml, get_db_connection, load_yaml, use_read_only_ledger,
)
import run_state
from run_state import (
    _is_agent_artifact, check_state_consistency, count_retry, create_run, dispatch_step,
    find_latest_artifact, get_next_step, init_run_state, load_run_state, parse_frontmatter,
    record_verdict, reset_retries, save_run_state,
)
from routing import (
    GATE_APPROVAL, GATE_ARBITER_ESCALATION, GATE_ARBITER_NEEDS_OWNER, GATE_ARBITER_UNREADABLE,
    GATE_BLOCKED, GATE_DEPLOY_APPROVAL, GATE_DEPLOY_FLAG_MISSING,
    GATE_FIX_ROUNDS, GATE_RETRY_LIMIT, GATE_RISK_UNANSWERED, ROUTE_ADVANCE, ROUTE_COMPLETE, ROUTE_COMPLETE_SKIPPING_OPS,
    ROUTE_OWNER_GATE, ROUTE_RETRY_LIMIT, SEND_BACK_GATE_KINDS, VERDICTS_RELAY_TESTS, _deploy_flag,
    decide_routing, determine_next, fix_round_summary, moves_past, send_back_target,
    unconfirmed_run_reason,
)
from agents import artifact_name, build_prompt
import owner_notes
from owner_notes import owner_notes_prompt, read_owner_notes
from recorded_runs import (
    _test_command_awaits_owner, _test_run_prompt, ensure_test_run, is_test_command_confirmed,
    latest_test_run, read_test_runs,
)
from read_api import (
    ERROR_INTERNAL, ERROR_LEDGER_UNAVAILABLE, ERROR_REPO_NOT_FOUND, ERROR_TASK_NOT_FOUND,
    GATE_READY, GATE_WAITING_ON_AGENT, RepoNotFound, TaskNotFound, error_payload,
    gate_check_payload, list_payload, print_json, profile_payload, read_interruptions,
    read_latest_step, status_payload,
)


# The most of one problem with a profile that is printed. A profile is
# written by a branch, and what is wrong with it can quote it, so each
# problem goes through gate_card.printable. This is long enough for
# the sentence Relay writes about an unknown key (225 characters besides
# the key) and for YAML's own errors, which quote a short piece of the
# line (the longest one seen was 233 characters); a long name
# from the profile is cut.
PROFILE_PROBLEM_PRINT_LIMIT = 400

# The most of a routing or gate reason that is printed. Relay writes the
# reason, but it can quote a report's phase and verdict, so it goes through
# gate_card.printable. Relay's own reasons are far shorter; a long verdict
# quoted in one is cut.
REASON_PRINT_LIMIT = 2000


def _accept_test_command(task_id: str, state: dict, to_phase: str, gate: dict,
                         config: dict) -> None:
    """Called by `relay override`: if the task is waiting for the owner to
    accept a changed test command and the override moves it forward to the
    phase that gate offered, record the acceptance. An override to anywhere
    else (back to the Builder, say) accepts nothing. At an Arbiter step,
    nothing is accepted until `relay next` has recorded the ruling the gate
    is built from, so a report rewritten before Relay read it offers
    nothing. Where the Arbiter ruled to move on, the gate is the report it
    ruled on's, with that step's run. At an Arbiter escalation (`gate` is
    the gate the override answers), only taking one of its choices that
    moves past that report accepts, and only where the gate named what the
    run flags: nothing is accepted that the owner was not shown."""
    latest_step = read_latest_step(task_id, state, config)
    if not latest_step or latest_step["verdict"] is None:
        return
    phase, verdict, step = latest_step["phase"], latest_step["verdict"], latest_step["step"]
    if phase == ARBITER:
        if arbiter_record.recorded_report(task_id, step, config) is None:
            return
        trigger = arbiter.trigger_of(state.get("history"), step)
        if trigger is None:
            return
        phase, verdict, step = trigger.phase, trigger.verdict, trigger.step
    test_run = latest_test_run(task_id, config, step)
    if not _test_command_awaits_owner(task_id, config, phase, verdict, test_run):
        return
    if gate["kind"] == GATE_ARBITER_ESCALATION:
        if not (_names_the_run(gate["arbiter"]["needs_owner_because"], test_run)
                and to_phase in _phases_offered_past(gate["arbiter"]["choices"], phase)):
            return
    elif to_phase != determine_next(phase, verdict, config)[0]:
        return
    harness_digest = test_run.get("harness_digest") if test_run.get("harness_changed") else None
    tests_run = test_run.get("tests_run") if test_run.get("tests_drop") else None
    harness_unchecked_at = test_run.get("commit") if test_run.get("harness_unchecked") else None
    db.record_test_command_accepted(get_db_connection(), task_id, phase=phase,
                                    step=step, command=test_run["command"],
                                    test_report=test_run.get("test_report"),
                                    commit=test_run.get("commit"), harness_digest=harness_digest,
                                    tests_run=tests_run, harness_unchecked_at=harness_unchecked_at)
    commit = gate_card.printable(test_run.get("commit") or "this commit")[:12]
    if test_run.get("command_differs_from_base"):
        print(f"Accepted this task's test command: {test_run['command']}")
    if harness_digest is not None:
        print(f"Accepted this task's harness files as they are at {commit}.")
    if harness_unchecked_at is not None:
        print(f"Accepted this task's unchecked harness at {commit}; a later commit asks again.")
    if tests_run is not None:
        print(f"Accepted {tests_run} tests run; a later run with fewer asks again.")


def _phases_offered_past(choices: list[dict], trigger_phase: str) -> set[str]:
    """The phases an escalation's choices move to past the report the
    Arbiter ruled on."""
    return {choice["phase"] for choice in choices
            if choice["does"] == arbiter.DOES_MOVE_TO and moves_past(choice["phase"], trigger_phase)}


def _names_the_run(needs_owner_because, test_run: dict | None) -> bool:
    """Whether an Arbiter escalation's warnings give Relay's reason that
    this passing run awaits the owner's confirmation. Read from what the
    gate says, so what counts as shown is exactly what the owner was shown."""
    reason = unconfirmed_run_reason(test_run, confirmed=False)
    return reason is not None and reason in (needs_owner_because or ())


# ---------------------------------------------------------------------------
# The task's checkout
# ---------------------------------------------------------------------------
# Relay does not make worktrees: the session that builds makes its own (Claude
# Code does), and `relay start` records the checkout it is given. Every git
# call here is an argument list, never shell=True.

class CheckoutError(Exception):
    """The checkout `relay start` was given cannot be the task's."""


# Task ids must be safe as a path segment and a lock file name: start and end
# alphanumeric, interior may use '.', '_', '-'. The interior '..' sequence and
# a trailing '.lock' are rejected separately.
_SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def sanitize_task_id(task_id: str) -> str:
    """Return task_id unchanged if it is safe for paths. Raises ValueError
    otherwise, so a task id cannot traverse paths ('../') or carry shell
    metacharacters."""
    if not task_id or not isinstance(task_id, str):
        raise ValueError("task id must be a non-empty string")
    if not _SAFE_TASK_RE.match(task_id):
        raise ValueError(
            f"unsafe task id '{task_id}': use only letters, digits, '.', '_', '-', "
            f"and start and end with a letter or digit"
        )
    if ".." in task_id:
        raise ValueError(f"unsafe task id '{task_id}': '..' is not allowed")
    if task_id.endswith(".lock"):
        raise ValueError(f"unsafe task id '{task_id}': '.lock' suffix is not allowed")
    return task_id


def _git(cwd, *args: str) -> str | None:
    """git's output, stripped, or None when git fails."""
    try:
        done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _default_base(repo_path: Path) -> base_trust.Base:
    """The repo's default branch. Not finding one is the checkout's
    problem (CheckoutError), so the --worktree hint is printed with it."""
    try:
        return base_trust.resolve(repo_path, None)
    except base_trust.BaseRefused as refused:
        raise CheckoutError(str(refused)) from None


def _common_dir(path) -> str | None:
    """The real path of the git folder a checkout and its worktrees share."""
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(common) if common else None


def task_checkout(repo_path: Path, worktree: str | None, base: str | None) -> tuple[dict, base_trust.Base]:
    """({"worktree_path", "branch", "base"}, the base) for the checkout a
    task builds in: `worktree`, or the current folder. It must be a
    checkout of the registered repo (the repo itself or one of its
    worktrees), on a branch of its own: not a detached HEAD, and not the
    default branch. A base Relay cannot take raises BaseRefused."""
    given = Path(worktree) if worktree else Path.cwd()
    where = str(given) if worktree else f"the current folder ({given})"
    top = _git(given, "rev-parse", "--show-toplevel") if given.is_dir() else None
    if top is None:
        raise CheckoutError(f"{where} is not a git checkout")
    repo_common = _common_dir(repo_path)
    if repo_common is None or _common_dir(top) != repo_common:
        raise CheckoutError(f"{where} is not a checkout of {repo_path}")
    branch = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise CheckoutError(f"{top} has a detached HEAD; check out the task's branch first")
    if proto.is_proto_branch(branch):
        raise CheckoutError(f"{top} is on the prototype branch '{branch}', and prototype code is "
                            "never merged; promote the prototype and build on a fresh branch")
    resolved_base = base_trust.resolve(repo_path, base) if base is not None else _default_base(repo_path)
    default = _default_base(repo_path)
    if branch in {default.name, default.branch, resolved_base.name, resolved_base.branch}:
        raise CheckoutError(f"{top} is on '{branch}', the branch the task is based on; "
                            f"make a branch for the task first (git switch -c <name>)")
    return ({"worktree_path": os.path.realpath(top), "branch": branch, "base": resolved_base.name},
            resolved_base)


def cmd_start(task_id: str, auto: bool, config: dict, deploy_intent: bool = False,
              repo: str | None = None, base: str | None = None,
              worktree: str | None = None) -> int:
    """Start a new task run. With a registered git repo, the task builds in
    `worktree` (default: the current folder), which must be a checkout of
    that repo on a branch of its own; Relay records it and makes none.
    Returns the exit code."""
    # Validate the task id up front: it becomes a path segment.
    try:
        task_id = sanitize_task_id(task_id)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    task_dir = settings.TASKS_DIR / task_id
    brief = task_dir / "brief.md"
    if not brief.exists():
        print(f"Error: No task brief found.")
        print(f"  Central: {task_dir}/brief.md")
        return 1

    if load_run_state(task_id, config) is not None:
        print(f"Error: Task {task_id} already has a run. Use 'next' to advance or 'abort' to restart.")
        return 1

    task_dir.mkdir(parents=True, exist_ok=True)

    task_sizing = sizing.read_brief_sizing(task_id)
    if task_sizing.deploy is not None:
        deploy_intent = deploy_intent or task_sizing.deploy

    state = init_run_state(task_id)
    state["deploy_intent"] = deploy_intent
    state["repo"] = repo

    # Record the checkout the task builds in, when the repo is a registered
    # git repo. A task with no such repo runs in place.
    repo_path = _resolve_repo_path(repo) if repo else None
    trusted_base = None
    if repo_path and (repo_path / ".git").exists():
        try:
            checkout, trusted_base = task_checkout(repo_path, worktree, base)
        except CheckoutError as e:
            print(f"Error: {e}.")
            print(f"  Run it from the task's checkout, or give --worktree PATH.")
            return 1
        except base_trust.BaseRefused as refused:
            print(f"Error: {refused}.")
            return 1
        if _sqlite_enabled(config):
            sharing = db.open_tasks_in(get_db_connection(), checkout["worktree_path"])
            if sharing:
                print(f"Error: {', '.join(sharing)} still builds in {checkout['worktree_path']}; "
                      "two tasks in one checkout would test each other's changes.")
                print(f"  Give this task its own worktree (git worktree add -b <branch> <path>).")
                return 1
        try:
            base_commit = base_trust.confirm_at_start(repo_path, trusted_base)
        except base_trust.BaseRefused as refused:
            print(f"Error: {refused}.")
            return 1
        state.update(checkout)
        state["base_ref"], state["base_commit"] = trusted_base.ref, base_commit
    elif worktree:
        print(f"Error: --worktree needs --repo with a registered git repo.")
        return 1
    elif base:
        print(f"Warning: --base '{base}' ignored (no checkout recorded for this task).")

    step = 1
    first = sizing.first_phase(task_sizing)
    with run_state.ledger_writes(task_id, config):
        create_run(task_id, state, config)
        if trusted_base is not None:
            base_trust.record(task_id, state, trusted_base, state["base_commit"], "start", None, config)
        sizing.record(task_id, task_sizing, config)
        state["current_phase"] = first
        name = artifact_name(first, step, state)
        prompt = build_prompt(first, task_id, step, name, state=state, config=config)
        dispatch_step(task_id, state, first, step, name, config)
        save_run_state(task_id, state, config)

    print(f"Task {task_id} started.")
    print(f"  Phase: {first}")
    print(f"  {sizing.summary(task_sizing)}")
    print(f"  Artifact: {name}")
    print(f"  Deploy intent: {deploy_intent}")
    if state.get("worktree_path"):
        print(f"  Worktree: {state['worktree_path']}")
        print(f"  Branch:   {state['branch']} (base {state['base']} at "
              f"{base_trust.short(state['base_commit'])})")

    return agents.invoke_agent(first, task_id, prompt, config, auto, cwd=_task_cwd(state),
                               step=step)


def _note_sleep_during_step(task_id: str, state: dict, phase: str, step, artifact: Path,
                            config: dict) -> None:
    """If the machine slept between this step being dispatched and its
    artifact being written, print it and record it once, so a step that took
    hours is not mistaken for hours of work. A sleep after the artifact was
    written (a lid closed overnight before `relay next` was run) is not the
    step's and is not reported."""
    if not isinstance(step, int):
        return
    dispatched = next((entry.get("timestamp") for entry in reversed(state.get("history") or [])
                       if isinstance(entry, dict) and entry.get("step") == step), None)
    try:
        started = datetime.strptime(str(dispatched), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        finished = datetime.fromtimestamp(artifact.stat().st_mtime, timezone.utc)
    except (ValueError, OSError):
        return
    sleeps = awake.sleeps_between(started, finished)
    if not sleeps:
        return  # none, or the power log could not be read
    total = sum(sleep["asleep_seconds"] for sleep in sleeps)
    times = "once" if len(sleeps) == 1 else f"{len(sleeps)} times"
    print(f"  Note: this Mac went to sleep {times}, for {total // 60} minutes in all, between "
          f"{gate_card.printable(phase)} (step {step}) being dispatched and its artifact being written. "
          f"The time this step took includes that.")
    if not _sqlite_enabled(config):
        return
    if any(noted["step"] == step for noted in read_interruptions(task_id, config)):
        return
    db.record_slept_during_step(
        get_db_connection(), task_id, phase=phase, step=step,
        sleep={"sleeps": len(sleeps), "asleep_seconds": total,
               "first_slept_at": sleeps[0]["slept_at"], "last_slept_at": sleeps[-1]["slept_at"]})


def _limit_stop_words(phase: str, state: dict, config: dict) -> str:
    """Why the limit stopped the task: the automatic rounds that ran, which
    is not the limit when the limit was lowered under the task."""
    rounds = state.get("retries", {}).get(phase, 0)
    times = "1 time" if rounds == 1 else f"{rounds} times"
    return (f"{phase} has gone back for fixes {times} in a row "
            f"with nobody deciding (limit {config.get('max_retries', 3)}).")


def _print_limit_choices(task_id: str, send_back_recommended: bool = False) -> None:
    """What the owner can do with a task the limit on automatic rounds
    stopped. No stop Relay makes recommends a send-back; the flag is
    kept for a stop whose gate says so."""
    recommended = "recommended; " if send_back_recommended else ""
    print(f"  Read the latest report, then choose:")
    print(f"    {_CLI_CMD} send-back {task_id} --note \"what to fix\"   ({recommended}one more round; the count starts again)")
    print(f"    {_CLI_CMD} override {task_id} <phase>                 (move it somewhere else)")
    print(f"    {_CLI_CMD} abort {task_id}")


def _print_fix_rounds(listed: dict) -> None:
    """A standing fix_rounds gate's list: the sentence that opens it, then a line per round."""
    opening, *rounds = listed["lines"]
    print(f"  {opening}")
    _print_lines(rounds, indent="    ")


def _review_loops(task_id: str, state: dict, config: dict) -> int:
    """The review loops on the current plan (arbiter.loop_counts)."""
    return arbiter.loop_counts(state.get("history"), read_test_runs(task_id, config),
                               VERDICTS_RELAY_TESTS).review_loops_since_replan


def _print_review_loops(loops: int, task_id: str, config: dict) -> None:
    """Where the retry limit stopped a route the Arbiter would have taken:
    how many review loops ran, and the Arbiter's latest ruling, if any."""
    print(f"  {'1 review loop' if loops == 1 else f'{loops} review loops'} on this plan.")
    latest = _latest_ruling(task_id, config)
    if latest:
        print(f"  The Arbiter last ruled (step {latest['step']}): "
              f"{arbiter_record.DECISION_WORDS[latest['decision']]}.")


def _latest_ruling(task_id: str, config: dict) -> dict | None:
    rulings = [ruling for ruling in arbiter_record.read_rulings(task_id, config) if ruling["decision"]]
    return rulings[-1] if rulings else None


def _arbiter_gate_words(decision) -> str:
    """The HUMAN GATE line of a gate the Arbiter's ruling stopped at, in
    Relay's words."""
    if decision.gate_kind == GATE_ARBITER_ESCALATION:
        warns = (f" Relay warns: {'; '.join(decision.needs_owner_because)}."
                 if decision.needs_owner_because else "")
        return "the Arbiter asks you to decide." + warns
    if decision.gate_kind == GATE_ARBITER_NEEDS_OWNER:
        return f"the Arbiter's ruling needs your yes: {'; '.join(decision.needs_owner_because)}."
    return f"Relay could not act on the Arbiter's decision: {decision.arbiter_problem}"


def _print_fix_round_gate(task_id: str, artifact: Path, listed: dict) -> None:
    """A fix_rounds gate left standing from before the Arbiter. It
    recommends nothing: ending a loop is not "do another round"."""
    print(f"\n  HUMAN GATE: fix rounds are still finding highs.")
    print(f"  Review the artifact at: {artifact}")
    _print_fix_rounds(listed)
    print(f"\n  To send it back to the builder for fixes:")
    print(f"    {_CLI_CMD} send-back {task_id}")
    print(f"\n  To move it somewhere else:")
    print(f"    {_CLI_CMD} override {task_id} <phase>")
    print(f"\n  To stop:")
    print(f"    {_CLI_CMD} abort {task_id}")


def _print_paused(task_id: str) -> None:
    print(f"Task {task_id} is blocked: it was paused.")
    print(f"  To continue: {_CLI_CMD} override {task_id} <phase>")
    print(f"  To stop:     {_CLI_CMD} abort {task_id}")


def _role_in_words(phase: str) -> str:
    """The role as a sentence names it: "the reviewer", "Security"."""
    return str(phase).capitalize() if phase in ("security", "ops") else f"the {phase}"


def _print_lines(lines: list[str], indent: str = "") -> None:
    for line in lines:
        print(f"{indent}{line}")


@dataclass(frozen=True)
class FinishedStep:
    """The latest report `relay next` acts on: its file, its frontmatter,
    and the phase and verdict read from it."""
    path: Path
    frontmatter: dict
    phase: str
    verdict: str


def cmd_next(task_id: str, auto: bool, config: dict, force_phase: str | None = None) -> int:
    """Parse the latest artifact and advance the workflow. Returns the
    exit code; the CLI edge (relay.py) turns it into the process's exit."""
    state = load_run_state(task_id, config)
    refusal = _refusal_before_reading(task_id, state, config)
    if refusal is not None:
        return refusal
    # Before anything is recorded: a base Relay cannot confirm stops here.
    refusal = base_trust.check_before_step(task_id, state, config)
    if refusal is not None:
        return refusal
    # Read before the verdict is: reading the latest report records its
    # verdict in the ledger. True means an earlier `relay next` stopped here.
    already_routed = _latest_verdict_recorded(state)
    finished = _read_latest_verdict(task_id, state, config)
    if not isinstance(finished, FinishedStep):
        return finished
    _end_the_steps_hold(task_id, state, finished, config)
    with run_state.ledger_writes(task_id, config):  # the deploy decision and its save land together
        planned_deploy = _apply_architect_deploy_flag(task_id, state, finished, config)
        _record_the_verdict(task_id, state, finished, config)
    test_run = _run_the_tests(task_id, state, finished, config)
    _risk_hint_after_build(task_id, state, finished, config)
    judgment = _judgment_of_the_step(task_id, state, finished, config)
    decision = _decide_where_next(task_id, state, finished, config, planned_deploy=planned_deploy,
                                  force_phase=force_phase, test_run=test_run,
                                  already_routed=already_routed, judgment=judgment)
    _record_the_arbiter_outcome(task_id, state, finished, decision, config)
    _record_the_sizing_judgment(task_id, judgment, config)
    return _act_on_routing(task_id, state, decision, finished, test_run, config, auto,
                           judgment=judgment)


def _latest_verdict_recorded(state: dict) -> bool:
    """True when the run state's latest step already has its verdict."""
    entries = [entry for entry in state.get("history") or [] if isinstance(entry, dict)]
    return bool(entries) and bool(entries[-1].get("verdict"))


def _refusal_before_reading(task_id: str, state: dict | None, config: dict) -> int | None:
    """The exit code when the task cannot move on at all, after printing
    why: it has no run (1), it is already closed (0), it is blocked (1), or
    a report's file name is one printable() would change (1). None when it
    can move on. Refuses before anything is recorded."""
    if not state:
        print(f"Error: No run state for {task_id}. Use 'start' first.")
        return 1
    if state.get("status") in ("completed", "abandoned"):
        print(f"Task {task_id} is already {state['status']}.")
        return 0
    if state.get("status") == "blocked":
        _refuse_blocked(task_id, state, config)
        return 1
    if _refused_for_a_report_name(task_id, config, f"{_CLI_CMD} next {task_id}"):
        return 1
    return None


def _report_names_printable_would_change(task_id: str, config: dict) -> list[str]:
    """Files backend: the numbered reports in the task folder whose file
    name printable() would change. A role chooses its report's name there,
    and Relay puts it into the prompt it prints for the owner to paste,
    where a cleaned name would hand the next role a path that does not
    exist. Under sqlite the names Relay uses are its own, from the ledger."""
    task_dir = settings.TASKS_DIR / task_id
    if _sqlite_enabled(config) or not task_dir.is_dir():
        return []
    return sorted(entry.name for entry in task_dir.iterdir()
                  if _is_agent_artifact(entry) and gate_card.printable(entry.name) != entry.name)


def _refused_for_a_report_name(task_id: str, config: dict, command: str) -> bool:
    """True, after naming each such file cleaned and asking for it to be
    renamed, when a report's file name printable() would change. Relay then
    reads none of the task's reports, so it neither prints the name raw nor
    routes on an older report in its place. `command` is what to run again."""
    names = _report_names_printable_would_change(task_id, config)
    if not names:
        return False
    print("Warning: Relay will not read this task's reports while a report's file name cannot "
          "be printed as it is:")
    for name in names:
        print(f"  {gate_card.printable(name)}  (the name as Relay would print it)")
    print(f"  The name holds a character a terminal acts on or does not show, a run of spaces, "
          f"or is over {gate_card.QUOTED_LIMIT} characters.")
    print(f"  Rename the file to a plain name, then run '{command}' again.")
    return True


def _refuse_blocked(task_id: str, state: dict, config: dict) -> None:
    """Say why a blocked task (paused, stopped at the limit on automatic
    rounds, or stopped by a role's time limit) does not move: it moves
    again only when the owner says so."""
    gate = read_api.read_gate(task_id, state, read_latest_step(task_id, state, config), config)
    if gate["kind"] == read_api.GATE_TIME_LIMIT:
        stop = gate["time_limit"]
        print(f"Task {task_id} is stopped.")
        _print_lines(agents.time_limit_stop_lines(task_id, stop["phase"], stop["limit_seconds"]),
                     indent="  ")
    elif gate["kind"] == GATE_RETRY_LIMIT:
        print(f"Task {task_id} is stopped: {_limit_stop_words(gate['next_phase'], state, config)}")
        if "fix_rounds" in gate:
            _print_fix_rounds(gate["fix_rounds"])
        if "review_loops" in gate:
            _print_review_loops(gate["review_loops"], task_id, config)
        _print_limit_choices(task_id, gate["send_back_recommended"])
    else:
        _print_paused(task_id)
        print(f"  It stopped in phase: {state.get('current_phase')}")
    _stop_for_owner(task_id, config)


def _read_latest_verdict(task_id: str, state: dict, config: dict) -> FinishedStep | int:
    """The latest report and its verdict, or the exit code after saying
    what is missing: no report at all, or a report with no verdict (1). An
    Arbiter step with no verdict Relay can read stops for the owner (0)."""
    latest_path, frontmatter = find_latest_artifact(task_id, config)
    if not latest_path:
        print(f"No artifacts found for {task_id}. Run the first agent.")
        return 1

    verdict = frontmatter.get("verdict")
    phase = frontmatter.get("phase", state.get("current_phase"))
    if not verdict and phase == ARBITER:
        return _stop_at_an_unreadable_arbiter(task_id, state, latest_path, config)
    if not verdict:
        valid = VALID_VERDICTS.get(phase, [])
        print(f"Warning: No verdict found in {gate_card.printable(latest_path.name)}")
        print(f"  The artifact may be missing YAML frontmatter.")
        print(f"  Current phase: {gate_card.printable(phase)}")
        print(f"  Expected one of: {', '.join(valid)}")
        print(f"\n  Options:")
        print(f"    1. Fix the artifact and run 'next' again")
        print(f"    2. Use 'override {task_id} <phase>' to manually advance")
        return 1
    return FinishedStep(latest_path, frontmatter, phase, verdict)


def _unreadable_arbiter_problem(state: dict, path: Path) -> str:
    """Why Relay cannot act on the Arbiter's report, read from the file
    itself (the ledger keeps no verdict it could not accept)."""
    if not path.exists():
        return "its report is not there"
    if not path.is_file():
        return "its report is not a file"
    trigger = arbiter.trigger_of_latest(state.get("history"))
    if trigger is None:
        return "Relay found no report the Arbiter was ruling on"
    _, problem = arbiter.read_ruling(parse_frontmatter(path), trigger.situation)
    return problem or "its report has no verdict Relay can read"


def _stop_at_an_unreadable_arbiter(task_id: str, state: dict, path: Path, config: dict) -> int:
    """An Arbiter step whose report is missing, or whose verdict is not one
    of its four: say why, record it once, and stop for the owner. Nothing
    moves. Returns the exit code, 0, as every stop for the owner does."""
    problem = _unreadable_arbiter_problem(state, path)
    step = arbiter.latest_step(state.get("history"))
    if _sqlite_enabled(config) and not _unreadable_already_recorded(task_id, step, problem, config):
        db.record_arbiter_unreadable(get_db_connection(), task_id, step=step, report=path.name,
                                     problem=problem)
    print(f"Task: {task_id}")
    print(f"\n  HUMAN GATE: Relay could not act on the Arbiter's decision: {problem}")
    print(f"  The Arbiter's report: {path.parent / gate_card.printable(path.name)}")
    _stop_for_owner(task_id, config)
    return 0


def _unreadable_already_recorded(task_id: str, step, problem: str, config: dict) -> bool:
    return any(ruling["step"] == step and ruling["problem"] == problem
               for ruling in arbiter_record.read_rulings(task_id, config))


def _end_the_steps_hold(task_id: str, state: dict, finished: FinishedStep, config: dict) -> None:
    """The phase has finished: let go of its hold on the machine, and say
    so if the machine slept while the step was open."""
    awake.release(settings.TASKS_DIR / task_id)
    _note_sleep_during_step(task_id, state, finished.phase, finished.frontmatter.get("step"),
                            finished.path, config)


def _apply_architect_deploy_flag(task_id: str, state: dict, finished: FinishedStep,
                                 config: dict) -> bool | None:
    """The Architect decides whether the task deploys; its plan's `deploy`
    flag overrides the --deploy guess given at start. Read it from the
    artifact file itself: the sqlite backend's step record keeps only
    phase and verdict. Returns the plan's flag (None when the step is not
    the Architect's or the plan does not say) and records a change."""
    if finished.phase != "architect":
        return None
    planned_deploy = _deploy_flag(parse_frontmatter(finished.path).get("deploy"))
    previous_deploy = bool(state.get("deploy_intent"))
    if planned_deploy is not None and planned_deploy != previous_deploy:
        print(f"  Architect set deploy: {'yes' if planned_deploy else 'no'}")
        state["deploy_intent"] = planned_deploy
        if _sqlite_enabled(config):
            db.record_deploy_decision(get_db_connection(), task_id,
                                      step=finished.frontmatter.get("step"),
                                      previous=previous_deploy, decided=planned_deploy)
    return planned_deploy


def _record_the_verdict(task_id: str, state: dict, finished: FinishedStep, config: dict) -> None:
    """Save the verdict on the step, print what was read, and print
    check_state_consistency's warnings (without stopping)."""
    if _sqlite_enabled(config):
        recorded_step = _report_step_number(finished.frontmatter)
        if recorded_step is None:
            recorded_step = _latest_history_step(state)
        step_warning = None
    else:
        recorded_step, step_warning = _files_backend_step(finished)
    record_verdict(task_id, state, finished.phase, recorded_step, finished.verdict, config)
    save_run_state(task_id, state, config)

    print(f"Task: {task_id}")
    print(f"  Latest artifact: {gate_card.printable(finished.path.name)}")
    print(f"  Phase: {gate_card.printable(finished.phase)}")
    print(f"  Verdict: {gate_card.printable(finished.verdict)}")
    if step_warning:
        print(f"  {step_warning}")

    check_state_consistency(task_id, state, finished.phase, recorded_step or 0)


def _files_backend_step(finished: FinishedStep) -> tuple[int, str | None]:
    """On the files backend, the step a report is: the number Relay gave
    its file name (05-builder-fix-1.md is step 5), never the `step:` line
    the role wrote. Returns that step and a warning when the role's own
    line says another step, else None."""
    named_step = int(finished.path.name[:2])
    claimed = finished.frontmatter.get("step")
    if claimed is None or _report_step_number(finished.frontmatter) == named_step:
        return named_step, None
    return named_step, (f"Warning: {gate_card.printable(finished.path.name)} says it is step "
                        f"{gate_card.printable(claimed)}, but its file "
                        f"name makes it step {named_step}. Its verdict is recorded on step "
                        f"{named_step}.")


def _report_step_number(frontmatter: dict) -> int | None:
    """The step number in a report's frontmatter: under sqlite the ledger's
    latest step, on the files backend the line the role wrote. None when it
    is missing or not a number."""
    step = frontmatter.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        return step
    if isinstance(step, str):
        try:
            return int(step)
        except ValueError:
            return None
    return None


def _latest_history_step(state: dict) -> int | None:
    """The step of the run state's latest history entry, if it has one."""
    entries = [entry for entry in state.get("history") or [] if isinstance(entry, dict)]
    return entries[-1].get("step") if entries else None


def _run_the_tests(task_id: str, state: dict, finished: FinishedStep, config: dict) -> dict | None:
    """Relay's own test run for the step, when the step calls for one,
    with its one-line summary printed. None when there is none. An Arbiter
    step runs nothing: its run is the one recorded at the report it ruled
    on, which is what a move-on is judged with."""
    if finished.phase == ARBITER:
        trigger = arbiter.trigger_of_latest(state.get("history"))
        return latest_test_run(task_id, config, trigger.step) if trigger else None
    test_run = ensure_test_run(task_id, state, finished.phase, finished.verdict,
                               finished.frontmatter.get("step"), config)
    if test_run:
        print(f"  {evidence.summary(test_run)}")
    return test_run


def _risk_hint_after_build(task_id: str, state: dict, finished: FinishedStep, config: dict) -> None:
    """After a Builder step that reports ready, work out the sizing hint
    for the whole branch, record it and print its one line, whatever the
    test run said."""
    if (finished.phase, finished.verdict) != ("builder", "ready-for-review"):
        return
    hint = risk_record.ensure_risk_hint(task_id, state, finished.frontmatter.get("step"), config)
    print(f"  {risk_hint.summary(hint)}")


def _judged_step(state: dict, finished: FinishedStep, config: dict) -> int | None:
    """The step number a report's sizing answer is recorded on, as its
    verdict is: the ledger's step under sqlite, the file name's on the
    files backend."""
    if not _sqlite_enabled(config):
        return _files_backend_step(finished)[0]
    step = _report_step_number(finished.frontmatter)
    return step if step is not None else _latest_history_step(state)


def _judgment_of_the_step(task_id: str, state: dict, finished: FinishedStep, config: dict):
    """What a Reviewer's or Arbiter's report says of the sizing, read from
    the report's own frontmatter (the ledger's step record keeps only the
    verdict); None for any other step."""
    if finished.phase not in sizing.JUDGING_ROLES:
        return None
    return risk_record.judgment_for_step(task_id, state, finished.phase,
                                         _judged_step(state, finished, config),
                                         read_api._frontmatter_or_empty(finished.path), config)


def _record_the_sizing_judgment(task_id: str, judgment, config: dict) -> None:
    """Record once what the report said of the sizing, and print it."""
    for line in risk_record.record_judgment(task_id, judgment, config):
        print(f"  {line}")


def _decide_where_next(task_id: str, state: dict, finished: FinishedStep, config: dict, *,
                       planned_deploy: bool | None, force_phase: str | None,
                       test_run: dict | None, already_routed: bool, judgment=None):
    """Routing's decision for the step (routing.py decides; this only
    gathers what it reads), with its reason printed."""
    test_runs = read_test_runs(task_id, config)
    arbiter_report = (_arbiter_report_to_route_on(task_id, finished, config)
                      if finished.phase == ARBITER else None)
    decision = decide_routing(state, finished.phase, finished.verdict, config,
                              planned_deploy=planned_deploy, force_phase=force_phase,
                              test_run=test_run,
                              test_command_confirmed=is_test_command_confirmed(task_id, config, test_run),
                              sizing=sizing.read_sizing(task_id, config),
                              arbiter_report=arbiter_report,
                              latest_task_test_run=test_runs[-1] if test_runs else None,
                              loop_counts=arbiter.loop_counts(state.get("history"), test_runs,
                                                              VERDICTS_RELAY_TESTS),
                              already_routed=already_routed, judgment=judgment)
    print(f"  Routing: {gate_card.printable(decision.reason, REASON_PRINT_LIMIT)}")
    return decision


def _arbiter_report_to_route_on(task_id: str, finished: FinishedStep, config: dict) -> dict:
    """The ruling Relay recorded for the step once it has acted on one,
    else the report's frontmatter (arbiter_record.recorded_report)."""
    recorded = arbiter_record.recorded_report(task_id, finished.frontmatter.get("step"), config)
    return recorded if recorded is not None else parse_frontmatter(finished.path)


def _record_the_arbiter_outcome(task_id: str, state: dict, finished: FinishedStep, decision,
                                config: dict) -> None:
    """For an Arbiter step: record once what Relay did with its ruling, or
    that it could not act on it, and print the Arbiter's reason, labelled
    as its words. The files backend keeps no events, so it records nothing."""
    if finished.phase != ARBITER:
        return
    step = finished.frontmatter.get("step")
    ruling, trigger = decision.ruling, decision.trigger
    if decision.architect_for_raise:
        # The raise to project sent the task to the Architect, so the ruling
        # was not acted on: it is recorded as not routed, and its
        # follow-ups are not read as accepted.
        trigger = arbiter.trigger_of_latest(state.get("history"))
        ruling = arbiter.read_ruling(_arbiter_report_to_route_on(task_id, finished, config),
                                     trigger.situation)[0] if trigger else None
    if ruling:
        print(f"  The Arbiter's reason (its words): "
              f"{gate_card.printable(ruling.reason, arbiter.MOST_REASON_CHARACTERS)}")
    if not _sqlite_enabled(config):
        return
    recorded = [found for found in arbiter_record.read_rulings(task_id, config)
                if found["step"] == step]
    if ruling and not any(found["decision"] for found in recorded):
        db.record_arbiter_ruling(get_db_connection(), task_id, step=step, report=finished.path.name,
                                 situation=trigger.situation,
                                 ruling=arbiter_record.ruling_record(ruling),
                                 routed=decision.outcome == ROUTE_ADVANCE and not decision.architect_for_raise,
                                 outcome=decision.reason)
    elif decision.arbiter_problem and not any(ruling["problem"] == decision.arbiter_problem
                                              for ruling in recorded):
        db.record_arbiter_unreadable(get_db_connection(), task_id, step=step,
                                     report=finished.path.name, problem=decision.arbiter_problem)


def _act_on_routing(task_id: str, state: dict, decision, finished: FinishedStep,
                    test_run: dict | None, config: dict, auto: bool, judgment=None) -> int:
    """Do what routing decided: close the task, stop for the owner (at a
    gate or at the limit on automatic rounds), or start the next phase.
    `judgment` is what the report said of the sizing, when it was a
    judging role's. Returns the exit code: 0, or the next agent's when it
    is run and fails."""
    if decision.outcome == ROUTE_COMPLETE:
        _close(state)
        save_run_state(task_id, state, config)
        print(f"\n  Workflow complete for {task_id}.")
        print(f"  Fill out 99-final-human-notes.md to close out.")
        return 0

    if decision.redirected_to_tester:
        print(f"\n  No Tester step since the last build (task predates the new order). "
              f"Running the Tester next.")

    if decision.outcome == ROUTE_COMPLETE_SKIPPING_OPS:  # Ops runs only for deploy tasks
        print(f"\n  No deploy intent set. Skipping ops.")
        print(f"  To run ops anyway: relay override {task_id} ops")
        _close(state)
        save_run_state(task_id, state, config)
        print(f"  Workflow complete. Fill out 99-final-human-notes.md.")
        return 0

    if decision.outcome in (ROUTE_OWNER_GATE, ROUTE_RETRY_LIMIT):
        _print_owner_gate(task_id, state, decision, finished.phase, finished.verdict,
                          finished.path, test_run, config)
        if decision.outcome == ROUTE_RETRY_LIMIT:
            state["status"] = "blocked"
            save_run_state(task_id, state, config)
        _stop_for_owner(task_id, config)
        return 0

    extra_prompt = (risk_record.raise_words(judgment, finished.path.name)
                    if decision.architect_for_raise and judgment
                    else _words_after_the_ruling(decision, finished))
    return _dispatch_next(task_id, state, decision.next_phase, test_run=test_run,
                          counts_as_retry=decision.counts_as_retry, config=config, auto=auto,
                          answers=_reports_this_round_answers(task_id, state, decision, finished,
                                                              test_run, config),
                          extra_prompt=extra_prompt)


def _words_after_the_ruling(decision, finished: FinishedStep) -> str:
    """What the role after an Arbiter's targeted round or re-plan is told,
    in the Arbiter's words, for that one step: never stored, so never handed
    to a later role."""
    if finished.phase != ARBITER or not decision.ruling:
        return ""
    if decision.ruling.decision == arbiter.TARGETED_ROUND:
        return arbiter_record.targeted_round_words(decision.ruling, finished.path)
    if decision.ruling.decision == arbiter.REPLAN:
        return arbiter_record.replan_words(decision.ruling, finished.path)
    return ""


def _reports_this_round_answers(task_id: str, state: dict, decision, finished: FinishedStep,
                                test_run: dict | None, config: dict) -> tuple[str, ...]:
    """What an automatic round back to the Builder answers: Relay's test
    run when it did not pass, else the report that sent it back (for an
    Arbiter's targeted round, its report and the report it ruled on).
    Nothing for any other round."""
    if decision.next_phase != "builder":
        return ()
    if test_run and test_run.get("outcome") in evidence.FAILING_OUTCOMES and test_run.get("log"):
        return (str(settings.TASKS_DIR / task_id / test_run["log"]),)
    if finished.phase == ARBITER and decision.trigger:
        ruled_on = read_api._step_artifacts(task_id, state, config).get(decision.trigger.step)
        return tuple(str(settings.TASKS_DIR / task_id / name)
                     for name in (finished.path.name, ruled_on) if name)
    if finished.phase in agents.RATING_PHASES:
        return (str(settings.TASKS_DIR / task_id / finished.path.name),)
    return ()


def _print_owner_gate(task_id: str, state: dict, decision, phase: str, verdict: str,
                      latest_path: Path, test_run: dict | None, config: dict) -> None:
    """What `relay next` prints where the decision stops for the owner: a
    gate, or the stop at the limit on automatic rounds. It prints only; the
    caller saves whatever the stop changes."""
    next_phase = decision.next_phase
    report = settings.TASKS_DIR / task_id / gate_card.printable(latest_path.name)
    if decision.outcome == ROUTE_RETRY_LIMIT:
        print(f"\n  STOPPED: {_limit_stop_words(next_phase, state, config)}")
        if decision.lists_fix_rounds:
            _print_fix_rounds(fix_round_summary(state))
        if decision.lists_review_loops:
            _print_review_loops(_review_loops(task_id, state, config), task_id, config)
        _print_limit_choices(task_id, decision.send_back_recommended)
        return
    if decision.gate_kind in (GATE_ARBITER_ESCALATION, GATE_ARBITER_NEEDS_OWNER,
                              GATE_ARBITER_UNREADABLE):
        print(f"\n  HUMAN GATE: {_arbiter_gate_words(decision)}")
        print(f"  Review the Arbiter's report at: {report}")
        if (decision.gate_kind == GATE_ARBITER_ESCALATION
                and _names_the_run(decision.needs_owner_because, test_run)):
            _print_how_testing_changed(test_run, ESCALATION_ACCEPTS)
        return
    if decision.gate_kind == GATE_DEPLOY_FLAG_MISSING:
        print(f"\n  HUMAN GATE: the plan doesn't say whether this task deploys.")
        print(f"  Add 'deploy: yes' or 'deploy: no' to the frontmatter of")
        print(f"    {report}")
        print(f"  then run: {_CLI_CMD} next {task_id}")
        print(f"  (Overriding past this keeps the start-time guess: deploy={'yes' if state.get('deploy_intent') else 'no'}.)")
        return
    if decision.gate_kind == GATE_FIX_ROUNDS:
        _print_fix_round_gate(task_id, report, fix_round_summary(state))
        return
    if decision.gate_kind == GATE_RISK_UNANSWERED:
        _print_risk_unanswered_gate(task_id, decision, phase, verdict, report, test_run, config)
        return
    print(f"\n  HUMAN GATE: Approval required.")
    print(f"  Review the artifact at: {report}")
    if decision.recommends == "accept":
        print(f"  The Arbiter ruled to move on and recommends accepting.")
    _print_the_run_at_the_gate(task_id, decision, phase, verdict, test_run, config)
    if decision.gate_kind == GATE_APPROVAL:
        rework_phase = send_back_target(phase)
        print(f"  Suggested next phase: {next_phase}")
        if decision.send_back_recommended:
            accept = "close the task" if next_phase == "done" else "continue"
            print(f"\n  To send it back to the {rework_phase} for fixes (recommended):")
            print(f"    {_CLI_CMD} send-back {task_id}")
            print(f"\n  To accept it as it is and {accept}:")
            print(f"    {_CLI_CMD} override {task_id} {next_phase}")
        else:
            print(f"\n  To approve and continue:")
            print(f"    {_CLI_CMD} override {task_id} {next_phase}")
            print(f"\n  To ask the {rework_phase} for changes:")
            print(f"    {_CLI_CMD} send-back {task_id} --note \"what to change\"")
    elif decision.gate_kind == GATE_DEPLOY_APPROVAL:
        print(f"\n  Ready to ship. Automated execute mode isn't wired up yet:")
        print(f"    deploy using the steps in the Ops artifact, run its smoke test,")
        print(f"    then close the task with: {_CLI_CMD} override {task_id} done")
    else:
        print(f"\n  Decide next action and use:")
        print(f"    {_CLI_CMD} send-back {task_id} --note \"your answer\"   "
              f"(back to {gate_card.printable(_role_in_words(send_back_target(phase)))})")
        print(f"    {_CLI_CMD} override {task_id} <phase>")
        print(f"    {_CLI_CMD} abort {task_id}")


# The most paths of one category `relay next` prints at the unanswered gate.
GATE_PATHS_PER_CATEGORY = 5


def _print_the_run_at_the_gate(task_id: str, decision, phase: str, verdict: str,
                               test_run: dict | None, config: dict) -> None:
    """An approval gate's lines on Relay's run: what a passing run asks
    the owner to confirm (which approving accepts), and what was seen to
    fail at base."""
    testing_phase, testing_verdict = ((decision.trigger.phase, decision.trigger.verdict)
                                      if phase == ARBITER and decision.trigger else (phase, verdict))
    if _test_command_awaits_owner(task_id, config, testing_phase, testing_verdict, test_run):
        _print_how_testing_changed(test_run)
    seen_to_fail = evidence.seen_to_fail_line(test_run) if test_run else None
    if seen_to_fail:
        print(f"  {gate_card.printable(seen_to_fail, PROFILE_PROBLEM_PRINT_LIMIT)}")


def _print_risk_unanswered_gate(task_id: str, decision, phase: str, verdict: str, report: Path,
                                test_run: dict | None, config: dict) -> None:
    """The gate where the Reviewer did not answer a sizing hint that
    matched: the hint, the approval gate's lines on Relay's run (an
    override past it accepts what they list), then the approval gate's
    choices and the re-size."""
    print(f"\n  HUMAN GATE: The Reviewer did not answer the risk hint.")
    print(f"  Review the artifact at: {report}")
    hint = risk_record.latest_risk_hint(task_id, config)
    print(f"  {risk_hint.summary(hint)}")
    _print_lines(risk_record.hint_lines(hint, GATE_PATHS_PER_CATEGORY), indent="    ")
    _print_the_run_at_the_gate(task_id, decision, phase, verdict, test_run, config)
    print(f"  Suggested next phase: {decision.next_phase}")
    print(f"\n  To approve and continue:")
    print(f"    {_CLI_CMD} override {task_id} {decision.next_phase}")
    print(f"\n  To ask the {send_back_target(phase)} for changes:")
    print(f"    {_CLI_CMD} send-back {task_id} --note \"what to change\"")
    print(f"\n  To re-size it:")
    print(f"    {_CLI_CMD} resize {task_id} --risky yes --reason \"...\"")


# Who accepts what the run flags, at an approval gate and at an Arbiter
# escalation (where only a choice that moves past the report ruled on does).
APPROVAL_ACCEPTS = "Approving"
ESCALATION_ACCEPTS = "A choice that moves on"


def _print_how_testing_changed(test_run: dict, accepts: str = APPROVAL_ACCEPTS) -> None:
    """The gate's lines for a passing run the owner must confirm: the
    changed command (in the words it has always had), the changed harness
    files and the drop in tests run, and what `accepts` accepts."""
    if test_run.get("command_differs_from_base"):
        print(f"\n  This task changed how the repo is tested. The passing run used:")
        print(f"    {test_run['command']}")
        print(f"  The base branch's test command: {test_run.get('base_command') or '(none)'}")
        if test_run.get("base_profile_unread"):
            why = gate_card.printable(test_run["base_profile_unread"], PROFILE_PROBLEM_PRINT_LIMIT)
            print(f"  {evidence.BASE_PROFILE_UNREAD}: {why}")
    else:
        print(f"\n  This task changed how the repo is tested.")
    harness, drop = test_run.get("harness_changed"), test_run.get("tests_drop")
    unchecked = test_run.get("harness_unchecked")
    if harness:
        print(f"  Harness files changed against the base: {evidence.listed_printable(harness)}")
    if unchecked:
        print(f"  Harness files not checked: {gate_card.printable(unchecked, PROFILE_PROBLEM_PRINT_LIMIT)}")
    if isinstance(drop, dict):
        print(f"  {gate_card.printable(evidence.drop_sentence(drop), PROFILE_PROBLEM_PRINT_LIMIT)}")
    if harness or drop or unchecked:
        print(f"  {accepts} accepts what is listed above for this task; a further change asks again.")
    else:
        print(f"  {accepts} accepts the new command for the rest of this task.")


def _dispatch_next(task_id: str, state: dict, next_phase: str, *, test_run: dict | None,
                   counts_as_retry: bool, config: dict, auto: bool,
                   answers: tuple[str, ...] = (), extra_prompt: str = "") -> int:
    """Start the phase routing chose: count the round when nobody chose it,
    record the step and run its agent. `extra_prompt` is said to this step
    only. The round, the step and the save commit together; the agent runs
    after. Returns the agent's exit code."""
    sizing_check = risk_record.sizing_prompt(task_id, state, next_phase, config)
    with run_state.ledger_writes(task_id, config):
        if counts_as_retry:
            count_retry(task_id, state, next_phase, config)
        step = get_next_step(task_id, config)
        name = artifact_name(next_phase, step, state)
        prompt = build_prompt(next_phase, task_id, step, name, state=state, config=config,
                              answers=answers)
        prompt += _test_run_prompt(task_id, test_run, next_phase)
        prompt += extra_prompt
        prompt += owner_notes_prompt(task_id, config)
        prompt += _what_this_role_is_handed(task_id, state, next_phase, step, config)
        prompt += sizing_check
        dispatch_step(task_id, state, next_phase, step, name, config)
        save_run_state(task_id, state, config)
        _record_the_arbiter_started(task_id, state, next_phase, step, name, config)

    print(f"\n  Next phase: {next_phase}")
    print(f"  Artifact: {name}")

    return agents.invoke_agent(next_phase, task_id, prompt, config, auto, cwd=_task_cwd(state),
                               step=step)


def _what_this_role_is_handed(task_id: str, state: dict, phase: str, step: int,
                              config: dict) -> str:
    """The Arbiter is handed the whole loop (arbiter_record.handover_prompt);
    every other role, what was accepted and logged on the task."""
    if phase == ARBITER:
        return arbiter_record.handover_prompt(task_id, state, step, config,
                                              read_api._step_artifacts(task_id, state, config))
    return arbiter_record.accepted_items_prompt(task_id, config)


def _record_the_arbiter_started(task_id: str, state: dict, phase: str, step: int, name: str,
                                config: dict) -> None:
    """The ledger's record that the Arbiter was dispatched, and on what."""
    if phase != ARBITER or not _sqlite_enabled(config):
        return
    trigger = arbiter.trigger_of(state.get("history"), step)
    if trigger is None:
        return
    db.record_arbiter_started(get_db_connection(), task_id, step=step, report=name,
                              situation=trigger.situation,
                              trigger={"step": trigger.step, "phase": trigger.phase,
                                       "verdict": trigger.verdict})


def cmd_status(task_id: str, config: dict):
    """Show current status of a task."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        return

    print(f"Task: {state.get('task_id', task_id)}")
    print(f"  Status: {state.get('status', 'unknown')}")
    print(f"  Phase: {state.get('current_phase', 'unknown')}")
    print(f"  Deploy intent: {state.get('deploy_intent', False)}")
    print(f"  Sizing:")
    _print_lines(risk_record.sizing_lines(task_id, config), indent="    ")
    print(f"  Created: {state.get('created', '?')}")
    print(f"  Updated: {state.get('updated', '?')}")

    for phase, count in state.get("retries", {}).items():
        if isinstance(count, int) and count > 0:
            print(f"  Automatic rounds: {phase} {count} of {config.get('max_retries', 3)}")

    history = state.get("history", [])
    if history:
        print(f"\n  History:")
        for entry in history:
            if isinstance(entry, dict):
                v = f" -> {gate_card.printable(entry['verdict'])}" if entry.get("verdict") else ""
                print(f"    {entry.get('step', '?'):02d} {entry.get('phase', '?')}{v} ({entry.get('timestamp', '?')})")

    owner_notes = read_owner_notes(task_id, config)
    if owner_notes:
        print(f"\n  The owner's notes:")
        for noted in owner_notes:
            first_line = noted["note"].splitlines()[0]
            more = " [...]" if len(noted["note"].splitlines()) > 1 else ""
            print(f"    {noted['timestamp']} ({noted['kind']}, at {noted['phase']}): {first_line}{more}")

    interruptions = read_interruptions(task_id, config)
    if interruptions:
        print(f"\n  The Mac slept while a step was open:")
        for noted in interruptions:
            print(f"    step {noted['step']} ({noted['phase']}): {noted.get('sleeps')} time(s), "
                  f"{(noted.get('asleep_seconds') or 0) // 60} minutes in all, "
                  f"first at {noted.get('first_slept_at')}")

    time_limit_stops = agents.read_time_limit_stops(task_id, config)
    if time_limit_stops:
        print(f"\n  The time limit stopped:")
        for stop in time_limit_stops:
            print(f"    step {stop['step']} ({stop['phase']}): ran "
                  f"{board_view._duration_words(stop.get('ran_seconds'))}, past its "
                  f"{agents.limit_in_words(stop.get('limit_seconds'))} limit, "
                  f"at {stop['stopped_at']}")

    test_runs = read_test_runs(task_id, config)
    if test_runs:
        print(f"\n  Test runs (Relay's own):")
        for run in test_runs:
            print(f"    after step {run['step']} ({run['phase']}): {evidence.summary(run)}")

    _print_builder_steps(arbiter.loop_counts(history, test_runs, VERDICTS_RELAY_TESTS))
    _print_rulings(arbiter_record.read_rulings(task_id, config))
    _print_accepted(arbiter_record.read_follow_ups(task_id, config))

    # Show artifacts
    task_dir = settings.TASKS_DIR / task_id
    if task_dir.exists():
        artifacts = sorted(f.name for f in task_dir.iterdir() if _is_agent_artifact(f))
        if artifacts:
            print(f"\n  Artifacts:")
            for a in artifacts:
                fm = parse_frontmatter(task_dir / a)
                verdict = fm.get("verdict", "")
                v_str = f" [{gate_card.printable(verdict)}]" if verdict else ""
                print(f"    {gate_card.printable(a)}{v_str}")


def _counts_line(counts: dict) -> str:
    by_role = ", ".join(f"{role} {count}" for role, count in counts["review_loop_by_role"].items()
                        if count)
    by_starter = ", ".join(f"{starter} {count}" for starter, count
                           in counts["review_loop_by_starter"].items() if count)
    loops = f"{counts[arbiter.CAUSE_REVIEW_LOOP]} review loops"
    if by_role:
        loops += f" (by role: {by_role}; started by: {by_starter})"
    return (f"{counts['total']} in all: {loops}, {counts[arbiter.CAUSE_FAILED_TEST_RUN]} after a "
            f"failed test run, {counts[arbiter.CAUSE_AFTER_REPLAN]} after a re-plan, "
            f"{counts[arbiter.CAUSE_OWNER_ELSEWHERE]} by the owner's override from elsewhere")


def _print_builder_steps(counts: "arbiter.LoopCounts") -> None:
    """`relay status`'s Builder steps by cause, for the task and since the
    latest re-plan: the same counts the Arbiter is handed."""
    if not counts.steps:
        return
    since = (f"since the re-plan at step {counts.replan_step}" if counts.replan_step is not None
             else "since the first build")
    print(f"\n  Builder steps:")
    print(f"    for the task: {_counts_line(counts.by_cause(False))}")
    print(f"    {since}: {_counts_line(counts.by_cause(True))}")


def _print_rulings(rulings: list[dict]) -> None:
    """`relay status`'s Arbiter section: how many rulings, then the latest,
    its reason labelled as the Arbiter's words."""
    if not rulings:
        return
    latest = rulings[-1]
    print(f"\n  The Arbiter: {len(rulings)} ruling(s)")
    if latest["decision"]:
        situation = gate_card.printable(latest["situation"] or "situation not recorded")
        print(f"    step {latest['step']} ({situation}): "
              f"{arbiter_record.DECISION_WORDS[latest['decision']]}")
        print(f"    Its reason (the Arbiter's words): "
              f"{gate_card.printable(latest['reason'] or '', arbiter.MOST_REASON_CHARACTERS)}")
    else:
        print(f"    step {latest['step']}: Relay could not act on it: "
              f"{gate_card.printable(latest['problem'] or '', arbiter.MOST_REASON_CHARACTERS)}")


def _print_accepted(items: list[dict]) -> None:
    """`relay status`'s accepted and logged items, each with its source."""
    if not items:
        return
    print(f"\n  Accepted and logged:")
    for item in items:
        source = "the Arbiter" if item["source"] == arbiter_record.SOURCE_ARBITER else "the owner"
        print(f"    step {item['step']}, {source}: "
              f"{gate_card.printable(item['text'], arbiter.MOST_ITEM_CHARACTERS)}")


def cmd_list(config: dict):
    """List all tasks and their status."""
    if _sqlite_enabled(config):
        states = db.list_tasks(get_db_connection())
        if not states:
            print("No tasks found.")
            return
        print(f"{'Task ID':<40} {'Status':<15} {'Phase':<15} {'Updated'}")
        print("-" * 90)
        for state in states:
            print(
                f"{state['task_id']:<40} {state.get('status', '?'):<15} "
                f"{state.get('current_phase', '?'):<15} {state.get('updated', '?')}"
            )
        return
    if not settings.TASKS_DIR.exists():
        print("No tasks found.")
        return
    tasks = sorted(d.name for d in settings.TASKS_DIR.iterdir() if d.is_dir() and (d / "run.yaml").exists())
    if not tasks:
        print("No tasks found.")
        return
    print(f"{'Task ID':<40} {'Status':<15} {'Phase':<15} {'Updated'}")
    print("-" * 90)
    for tid in tasks:
        state = load_run_state(tid, config)
        if state:
            print(
                f"{tid:<40} {state.get('status', '?'):<15} "
                f"{state.get('current_phase', '?'):<15} {state.get('updated', '?')}"
            )


def _close(state: dict) -> None:
    """Mark the task completed, as of now: the time a closed task shows is
    when it closed, not when it was last saved before."""
    state["status"] = "completed"
    state["current_phase"] = "done"
    state["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_override(task_id: str, phase: str, config: dict, auto: bool = False,
                 note: str | None = None, log_items: list[dict] | None = None) -> int:
    """Manually set the next phase (human override). `note` is what the owner
    says while doing it: recorded with the approval and given to later agents.
    `log_items` are the findings the owner accepts and logs as follow-ups
    (only `relay decide ... accept-and-log` gives them), recorded first.
    Returns the exit code."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        return 1

    allowed = PIPELINE_ORDER + ["done"]  # legacy phases are readable, not runnable
    latest_step = read_latest_step(task_id, state, config)
    if phase == ARBITER and (not latest_step or latest_step["phase"] != ARBITER):
        print(f"Invalid phase: {phase}. The Arbiter runs only where Relay calls it; it can be "
              f"started again only while its own step is the latest.")
        return 1
    if phase not in allowed and phase != ARBITER:
        print(f"Invalid phase: {phase}. Must be one of: {', '.join(allowed)}")
        return 1
    note, kept = _checked_note(note)
    if not kept:
        return 1
    # The Arbiter is handed every report's file name; no other override
    # prompt names a report from the folder.
    if phase == ARBITER and _refused_for_a_report_name(task_id, config,
                                                       f"{_CLI_CMD} override {task_id} {phase}"):
        return 1

    # Overriding out of a stop at the limit is the owner's decision: the
    # stopped phase's count starts again wherever the task goes.
    gate = read_api.read_gate(task_id, state, latest_step, config)
    return _override_into(task_id, state, phase, note, gate, config, auto, latest_step,
                          log_items=log_items)


def _override_into(task_id: str, state: dict, phase: str, note: str | None, gate: dict,
                   config: dict, auto: bool, latest_step: dict | None,
                   log_items: list[dict] | None = None) -> int:
    """The override itself, answering `gate` at `latest_step`: record that
    step's verdict if `relay next` has not, record the follow-ups the owner
    logged (`log_items`), record the override, start the counts the owner
    decided again, and start the phase (or close the task). Every write
    commits together; the agent runs after. Returns the exit code."""
    if phase != "done":
        refusal = base_trust.check_before_step(task_id, state, config)
        if refusal is not None:
            return refusal
    stopped_phase = gate["next_phase"] if gate["kind"] == GATE_RETRY_LIMIT else None
    sizing_check = risk_record.sizing_prompt(task_id, state, phase, config)
    with run_state.ledger_writes(task_id, config):
        _record_the_verdict_moved_past(task_id, state, latest_step, config)
        if _sqlite_enabled(config):
            if log_items:
                db.record_follow_ups(get_db_connection(), task_id, step=latest_step["step"],
                                     phase=latest_step["phase"], report=latest_step["artifact"],
                                     items=log_items)
            _accept_test_command(task_id, state, phase, gate, config)
            db.record_approval(get_db_connection(), task_id, phase=state.get("current_phase"),
                                next_phase=phase, method="override", note=note,
                                stopped_phase=stopped_phase)
        elif note:
            print("  Warning: the files backend keeps no events, so this note is not recorded.")
        if stopped_phase:
            reset_retries(state, stopped_phase)

        if phase == "done":
            _close(state)
            save_run_state(task_id, state, config)
        else:
            reset_retries(state, phase)  # the owner chose this round
            step = get_next_step(task_id, config)
            name = artifact_name(phase, step, state)
            prompt = build_prompt(phase, task_id, step, name, state=state, config=config)
            test_runs = read_test_runs(task_id, config)
            if test_runs:
                prompt += _test_run_prompt(task_id, test_runs[-1], phase)
            prompt += owner_notes.prompt_with(task_id, config, note)
            prompt += _what_this_role_is_handed(task_id, state, phase, step, config)
            prompt += sizing_check
            state["status"] = "in_progress"
            dispatch_step(task_id, state, phase, step, name, config)
            save_run_state(task_id, state, config)
            _record_the_arbiter_started(task_id, state, phase, step, name, config)

    if phase == "done":
        awake.release(settings.TASKS_DIR / task_id)
        print(f"Task {task_id} marked as completed.")
        return 0

    print(f"Overriding to phase: {phase}")
    print(f"  Artifact: {name}")

    return agents.invoke_agent(phase, task_id, prompt, config, auto, cwd=_task_cwd(state),
                               step=step)


def _record_the_verdict_moved_past(task_id: str, state: dict, latest_step: dict | None,
                                   config: dict) -> None:
    """The owner overrides past a report whose verdict `relay next` has not
    read yet: record it, as `relay next` and a send-back do, so every later
    check that reads the recorded verdicts (Security's high, for one) sees
    the report the owner moved past."""
    if not latest_step or latest_step["recorded"] or latest_step["verdict"] is None:
        return
    record_verdict(task_id, state, latest_step["phase"], latest_step["step"],
                   latest_step["verdict"], config)


def cmd_send_back(task_id: str, config: dict, auto: bool = False,
                  note: str | None = None) -> int:
    """Send the latest finished step back for more work (the owner's answer
    to a gate when the work is not good enough yet). Not an approval. It is
    the owner's decision, so the limit on automatic rounds never refuses it
    and the count for the phase it goes to starts again.

    Only allowed where gate-check offers it: the task is waiting on the
    owner at an approval or owner-decision gate, or the limit stopped it.
    Anywhere else it refuses and records nothing. Returns the exit code."""
    note, kept = _checked_note(note)
    if not kept:
        return 1
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        return 1
    if state.get("status") in ("completed", "abandoned"):
        print(f"Task {task_id} is already {state['status']}.")
        return 1

    if _refused_for_a_report_name(task_id, config, f"{_CLI_CMD} send-back {task_id}"):
        return 1
    latest_step = read_latest_step(task_id, state, config)
    gate = read_api.read_gate(task_id, state, latest_step, config)
    if gate["state"] == GATE_WAITING_ON_AGENT:
        phase = latest_step["phase"] if latest_step else state.get("current_phase")
        print(f"Nothing to send back: {phase} has not finished its artifact yet.")
        return 1
    if gate["state"] == GATE_READY:
        print(f"Nothing is waiting on you: '{_CLI_CMD} next {task_id}' will move on ({gate['reason']}).")
        print(f"  To redo a phase anyway: {_CLI_CMD} override {task_id} <phase>")
        return 1
    kind = gate["kind"]
    if kind == GATE_BLOCKED:
        _print_paused(task_id)
        return 1
    if kind not in SEND_BACK_GATE_KINDS or not gate["send_back_command"]:
        print(f"send-back does not apply at this gate ({gate['kind']}): {gate['reason']}")
        return 1

    phase = latest_step["phase"]
    # At the limit stop the round goes to the phase that was stopped.
    rework_phase = gate["next_phase"] if kind == GATE_RETRY_LIMIT else send_back_target(phase)
    return _send_back(task_id, state, latest_step, rework_phase, note, config, auto)


def _send_back(task_id: str, state: dict, latest_step: dict, rework_phase: str,
               note: str | None, config: dict, auto: bool) -> int:
    """The send-back itself: record it, start the phase's count again and
    run the phase that reworks the step. Returns the agent's exit code, or
    1 when the task's base cannot be confirmed (nothing recorded)."""
    refusal = base_trust.check_before_step(task_id, state, config)
    if refusal is not None:
        return refusal
    phase, verdict = latest_step["phase"], latest_step["verdict"]
    # The verdict, the new step, the save and the sent_back event commit
    # together: either the task is waiting on the agent with the send-back
    # recorded, or nothing changed. Once committed, running send-back again
    # is refused, so one decision can never be recorded twice.
    with run_state.ledger_writes(task_id, config):
        record_verdict(task_id, state, phase, latest_step["step"], verdict, config)
        reset_retries(state, rework_phase)
        state["status"] = "in_progress"

        step = get_next_step(task_id, config)
        name = artifact_name(rework_phase, step, state)
        ruled_on = _report_ruled_on(task_id, state, latest_step, config)
        answers = tuple(path for path in (latest_step.get("artifact_path"), ruled_on) if path)
        prompt = build_prompt(rework_phase, task_id, step, name, state=state, config=config,
                              answers=answers)
        prompt += _sent_back_words(latest_step, ruled_on)

        dispatch_step(task_id, state, rework_phase, step, name, config)
        save_run_state(task_id, state, config)
        _record_sent_back(task_id, config, phase, verdict, rework_phase, note)
        prompt += owner_notes.prompt_for_send_back(task_id, config, note)
    prompt += arbiter_record.accepted_items_prompt(task_id, config)
    prompt += risk_record.sizing_prompt(task_id, state, rework_phase, config)

    print(f"Sent back to {rework_phase}. Automatic rounds: 0 of {config.get('max_retries', 3)}.")
    if not note and _sqlite_enabled(config):  # the files backend has nowhere to keep a note
        print(f"No note recorded. Add one with: {_CLI_CMD} note {task_id} --note \"...\"")
    print(f"  Artifact: {name}")

    return agents.invoke_agent(rework_phase, task_id, prompt, config, auto,
                               cwd=_task_cwd(state), step=step)


def _report_ruled_on(task_id: str, state: dict, latest_step: dict, config: dict) -> str | None:
    """When the step sent back is the Arbiter's: the path of the report it
    ruled on. None for any other step."""
    if latest_step["phase"] != ARBITER:
        return None
    trigger = arbiter.trigger_of(state.get("history"), latest_step["step"])
    name = read_api._step_artifacts(task_id, state, config).get(trigger.step) if trigger else None
    return str(settings.TASKS_DIR / task_id / name) if name else None


def _sent_back_words(latest_step: dict, ruled_on: str | None) -> str:
    """What the agent doing a send-back is told to answer."""
    phase, verdict, path = latest_step["phase"], latest_step["verdict"], latest_step["artifact_path"]
    if phase != ARBITER:
        return (f"\n\nThis step was sent back by the owner. Answer what {path} says ({phase} "
                f"verdict: {gate_card.printable(verdict)}): fix what it raises, and do not redo "
                f"work it did not question.")
    ruling = (f"the Arbiter's ruling at {path} ({gate_card.printable(verdict)})" if verdict
              else f"the Arbiter's report at {path}, which could not be read")
    return (f"\n\nThis step was sent back by the owner, from {ruling}. Answer what "
            f"{ruled_on or 'the report it ruled on'} says: fix what it raises, and do not redo "
            f"work it did not question.")


def _record_sent_back(task_id: str, config: dict, phase: str, verdict: str, rework_phase: str,
                      note: str | None) -> None:
    """The ledger's record of a send-back. The older files backend keeps no
    events of any kind (approvals included), so there is nothing to write."""
    if _sqlite_enabled(config):
        db.record_sent_back(get_db_connection(), task_id, from_phase=phase, verdict=verdict,
                            to_phase=rework_phase, note=note)


def _checked_note(note: str | None) -> tuple[str | None, bool]:
    """(the owner's note, trimmed, or None when there is none; whether it
    can be kept). A note too long to keep is printed as a refusal, and the
    command stops before anything is recorded or moved."""
    note, problem = owner_notes.checked(note)
    if problem:
        print(problem)
        return note, False
    return note, True


def cmd_note(task_id: str, config: dict, note: str | None) -> int:
    """Record something the owner wants on the task's record without moving
    the task: a decision, a correction, a fact. Every later agent is given
    it. Allowed at any time, on any task that has been started. Returns
    the exit code."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        return 1
    note, kept = _checked_note(note)
    if not kept:
        return 1
    if not note:
        print(f"Nothing to record. Usage: {_CLI_CMD} note {task_id} --note \"what you decided\"")
        print(f"  A longer note can come from a file: {_CLI_CMD} note {task_id} --note-file notes.txt")
        return 1
    if not _sqlite_enabled(config):
        print("The files backend keeps no events, so a note cannot be recorded.")
        return 1
    latest_step = read_latest_step(task_id, state, config)
    with run_state.ledger_writes(task_id, config):
        db.record_owner_note(get_db_connection(), task_id, phase=state.get("current_phase"),
                             step=latest_step["step"] if latest_step else None, note=note)
    count = len(read_owner_notes(task_id, config))
    if state.get("status") in ("completed", "abandoned"):
        print(f"Note recorded on {task_id} ({count} from the owner so far). The task is "
              f"{state['status']}, so no agent will be given it; it is on the record.")
        return 0
    print(f"Note recorded on {task_id} ({count} from the owner so far). Later agents will be given it.")
    print(f"  An agent that is already running will not see it; the next one will.")
    return 0


def _resize_refusal(task_id: str, state: dict | None, size: str | None, risky: str | None,
                    reason: str | None, config: dict) -> str | None:
    """Why `relay resize` cannot record this, in one line, or None."""
    if not state:
        return f"No run state for {gate_card.printable(task_id)}."
    if state.get("status") in ("completed", "abandoned"):
        return f"Task {task_id} is already {state['status']}; its sizing can no longer change."
    if not _sqlite_enabled(config):
        return "Re-sizing is recorded in the ledger; this task is on the files backend."
    if size is None and risky is None:
        return f"Nothing to change: give --size fix|feature|project, --risky yes|no, or both."
    if size is not None and sizing.size_word(size) is None:
        return f"--size must be fix, feature or project, not {gate_card.printable(size, 40)!r}."
    if risky is not None and sizing.yes_or_no(risky) is None:
        return f"--risky must be yes or no, not {gate_card.printable(risky, 40)!r}."
    if not (reason or "").strip():
        return f"A re-size needs a reason: {_CLI_CMD} resize {task_id} ... --reason \"why\"."
    if len(" ".join(reason.split())) > sizing.MOST_REASON_CHARACTERS:
        return f"The reason must fit on one line of {sizing.MOST_REASON_CHARACTERS} characters."
    return None


def cmd_resize(task_id: str, config: dict, size: str | None, risky: str | None,
               reason: str | None) -> int:
    """The owner changes a task's sizing, either way, with a reason: the
    only way a sizing is ever lowered. Recorded as a further `sized` event
    with the trigger "owner". It changes no step that already ran, only
    which roles lie ahead. Each refusal is one line and exit 1, and
    records nothing. Returns the exit code."""
    try:
        state = load_run_state(sanitize_task_id(task_id), config)
    except ValueError:
        state = None
    refusal = _resize_refusal(task_id, state, size, risky, reason, config)
    if refusal:
        print(refusal)
        return 1
    wanted_risky = sizing.yes_or_no(risky) if risky is not None else None
    size = sizing.size_word(size) if size is not None else None
    before = sizing.read_sizing(task_id, config)
    after = sizing.owner_change(before, size, wanted_risky)
    if (after.size, after.risky) == (before.size, before.risky):
        print(f"Nothing to change: already size {before.size}, risky {sizing.yes_no(before.risky)}.")
        return 0
    reason = sizing.one_line_reason(reason)
    with run_state.ledger_writes(task_id, config):
        risk_record.owner_resize(task_id, state, size, wanted_risky, reason, config)
    words = sizing.change_words({"size": before.size, "risky": before.risky},
                                {"size": after.size, "risky": after.risky})
    print(f"Sizing changed: {words}. Reason: {reason}")
    return 0


# ---------------------------------------------------------------------------
# From the phone (gate_card.py): what waits, one gate's card, a report by
# name, and a choice made by its name for one step
# ---------------------------------------------------------------------------

def _stop_for_owner(task_id: str, config: dict) -> None:
    """Where `relay next` stops for the owner: the card, then one line that
    is the same at every gate, so a session driving Relay knows to ask the owner."""
    print()
    _print_lines(gate_card.card_lines(task_id, config, datetime.now()))
    print(gate_card.STOP_LINE)


def _read_only(config: dict) -> None:
    if _sqlite_enabled(config):
        use_read_only_ledger()


def cmd_waiting(config: dict):
    _read_only(config)
    _print_lines(gate_card.waiting_lines(config, datetime.now()))


def cmd_card(task_id: str, config: dict):
    _read_only(config)
    _print_lines(gate_card.card_lines(task_id, config, datetime.now()))


def cmd_show(task_id: str, name: str, config: dict) -> int:
    _read_only(config)
    text, problem = gate_card.report_text(task_id, name, config)
    if problem:
        print(problem)
        return 1
    _print_lines(gate_card.as_code_block(text.splitlines()))
    return 0


def _refuse(task_id: str, reason: str, config: dict) -> int:
    """Nothing is recorded: say why, then show the card as it is now.
    Returns the exit code, 1."""
    print(f"Not applied: {reason}")
    _print_lines(gate_card.card_lines(task_id, config, datetime.now()))
    return 1


def _offered_choice(task_id: str, status: dict, choice_name: str, step: int, gate_kind: str,
                    to_phase: str | None, note: str | None) -> tuple[gate_card.Choice | None, str]:
    """(the choice, "") if the task still waits on the owner at that step,
    at that kind of gate, and the card there offers it; (None, why not)
    for anything else, which is refused before anything is recorded, so
    a decision for a gate that has moved on is never applied. A note
    leaves the task at its gate, so the same note sent twice is recorded
    twice."""
    latest = status["latest_step"] or {}
    now_words = board_view.row_line(gate_card.row_of(status), datetime.now())
    if status["gate"]["state"] != read_api.GATE_WAITING_ON_OWNER or latest.get("step") != step:
        return None, f"{task_id} is no longer waiting at step {step}; it is now {now_words}."
    if status["gate"]["kind"] != gate_kind:
        return None, (f"{task_id} is no longer at the {gate_card.printable(gate_kind)} gate "
                      f"of step {step}; it is now {now_words}.")
    choice = next((offered for offered in gate_card.choices(status) if offered.name == choice_name),
                  None)
    if choice is None:
        return None, (f"{gate_card.printable(choice_name)} is not a choice at step {step} "
                      f"of {task_id}.")
    if choice.name == "move-to" and to_phase not in choice.targets:
        return None, f"move-to needs --to with one of: {', '.join(choice.targets)}."
    if choice.name != "move-to" and to_phase:
        return None, "--to is for move-to."
    if choice.action == gate_card.NOTE and not (note or "").strip():
        return None, "note needs --note or --note-file."
    return choice, ""


def _apply(choice: gate_card.Choice, task_id: str, to_phase: str | None, note: str | None,
           config: dict, auto: bool) -> int:
    """The CLI command the choice is, called exactly as `relay` calls it,
    and its exit code. Abort takes no note, so a note given with it is
    recorded first, by `relay note`, and a note refused stops the abort."""
    if note and choice.action == gate_card.ABORT:
        noted = cmd_note(task_id, config, note)
        if noted:
            return noted
    if choice.action == gate_card.OVERRIDE:
        return cmd_override(task_id, to_phase or choice.targets[0], config, auto, note=note,
                            log_items=list(choice.logs) or None)
    if choice.action == gate_card.SEND_BACK:
        return cmd_send_back(task_id, config, auto, note=note)
    if choice.action == gate_card.ABORT:
        return cmd_abort(task_id, config)
    return cmd_note(task_id, config, note)


@contextmanager
def _one_decision_at_a_time(task_id: str, config: dict):
    """Hold the task's decision lock while one decide reads its gate,
    checks the choice and applies it. A second decide on the task in that
    time is refused before it reads the gate or records anything, so two decisions
    sent at the same instant cannot both pass the check. The lock is in the
    `locks` folder beside the tasks folder (Relay's home), one file per task.
    It yields whether this decide holds the lock; when it does not, the
    caller refuses.
    The system lets go of it when the process ends, however it ends, so it
    is never left held. The file is kept: removing it would let a third
    decide lock a new file while the second still holds the old one. A
    task id that is not a task is refused first, so no file is made for it."""
    try:
        sanitize_task_id(task_id)
    except ValueError:
        raise TaskNotFound(f"No run state for {task_id}.")
    if not load_run_state(task_id, config):
        raise TaskNotFound(f"No run state for {task_id}.")
    lock_path = settings.TASKS_DIR.parent / "locks" / f"{task_id}.decide.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def cmd_decide(task_id: str, choice_name: str, step: int, gate_kind: str, to_phase: str | None,
               note: str | None, config: dict, auto: bool = False) -> int:
    """Make one choice from a task's card, by its name, for the step and
    the kind of gate the card was for. It runs the same command the owner would type at the Mac,
    so the ledger gets the same events and Relay's checks are the same;
    then it prints back what was recorded. One decide at a time per task.
    Returns the exit code."""
    with _one_decision_at_a_time(task_id, config) as held:
        if not held:
            return _refuse(task_id, f"another decision on {task_id} is under way. Read the "
                                    f"card again once it is done.", config)
        status = read_api.status_payload(task_id, config)
        choice, why_not = _offered_choice(task_id, status, choice_name, step, gate_kind,
                                          to_phase, note)
        if choice is None:
            return _refuse(task_id, why_not, config)
        notes_before = len(read_owner_notes(task_id, config))
        applied = _apply(choice, task_id, to_phase, note, config, auto)
        if applied:   # Relay's own refusal, already printed
            _print_lines(gate_card.card_lines(task_id, config, datetime.now()))
            return applied
        notes = read_owner_notes(task_id, config)
        _print_lines(gate_card.wrapped(_recorded_lines(task_id, step, choice, to_phase, config)))
        print(_recorded_note(notes[-1]["note"] if len(notes) > notes_before else None))
        return 0


def _recorded_lines(task_id: str, step: int, choice: gate_card.Choice, to_phase: str | None,
                    config: dict) -> list[str]:
    now_words = board_view.row_line(gate_card.row_of(read_api.status_payload(task_id, config)),
                                    datetime.now())
    return ["", "Recorded:", gate_card.decided_words(choice, to_phase),
            f"Task {task_id}, step {step}.", f"Now: {now_words}"]


def _recorded_note(recorded: str | None) -> str:
    """The note as the ledger holds it, shown whole: line breaks and inner
    spaces are kept, control characters are not shown."""
    if recorded:
        return f"Note: {gate_card.without_control_characters(recorded)}"
    return "No note."


def cmd_validate(task_id: str, config: dict) -> int:
    """Validate a task's brief, run state, and all artifacts. Returns the
    exit code: 1 when there are errors."""
    errors = []
    warnings = []

    brief = settings.TASKS_DIR / task_id / "brief.md"
    if not brief.exists():
        errors.append(f"No task brief found for {task_id}")
    else:
        # Check brief is not empty template
        content = brief.read_text()
        if "{title}" in content or "{YYYYMMDD" in content:
            warnings.append("Task brief appears to still contain template placeholders")

    task_dir = settings.TASKS_DIR / task_id
    if not task_dir.exists():
        errors.append(f"No handoff folder at {task_dir}")
        return _print_validation(task_id, errors, warnings)

    # Check run state (run.yaml under the files backend, the tasks table under sqlite)
    state = load_run_state(task_id, config)
    if not state:
        errors.append("No run.yaml found" if not _sqlite_enabled(config) else "No task row found in database")
        return _print_validation(task_id, errors, warnings)

    # Validate run.yaml fields
    for field in ["task_id", "created", "updated", "status", "current_phase", "deploy_intent"]:
        if field not in state:
            errors.append(f"run.yaml missing required field: {field}")

    if state.get("task_id") != task_id:
        errors.append(f"run.yaml task_id '{state.get('task_id')}' does not match folder '{task_id}'")

    status = state.get("status")
    if status and status not in VALID_STATUSES:
        errors.append(f"run.yaml status '{status}' is not valid. Expected: {', '.join(VALID_STATUSES)}")

    current = state.get("current_phase")
    if current and current not in VALID_PHASES + ["done"]:
        errors.append(f"run.yaml current_phase '{current}' is not valid")

    # Validate artifacts
    artifacts = sorted(
        [f for f in task_dir.iterdir() if _is_agent_artifact(f)],
        key=lambda f: f.name,
    )

    if not artifacts and state.get("status") == "in_progress":
        warnings.append("Task is in_progress but has no artifacts yet")

    seen_steps = []
    for art in artifacts:
        step_str = art.name[0:2]
        try:
            step_num = int(step_str)
        except ValueError:
            errors.append(f"{art.name}: filename does not start with a two-digit step number")
            continue

        if step_num in seen_steps:
            errors.append(f"{art.name}: duplicate step number {step_num:02d}")
        seen_steps.append(step_num)

        fm = parse_frontmatter(art)
        if not fm:
            errors.append(f"{art.name}: missing YAML frontmatter")
            continue

        # Check required frontmatter fields
        for field in ["task_id", "phase", "step", "date", "verdict"]:
            if field not in fm:
                errors.append(f"{art.name}: frontmatter missing '{field}'")

        art_phase = fm.get("phase")
        if art_phase and art_phase not in VALID_PHASES:
            errors.append(f"{art.name}: phase '{art_phase}' is not valid")

        # Validate verdict for phase
        art_verdict = fm.get("verdict")
        if art_phase and art_verdict:
            valid = VALID_VERDICTS.get(art_phase, [])
            if art_verdict not in valid:
                errors.append(
                    f"{art.name}: verdict '{art_verdict}' is not valid for phase '{art_phase}'. "
                    f"Expected: {', '.join(valid)}"
                )

        if fm.get("task_id") and fm["task_id"] != task_id:
            errors.append(f"{art.name}: frontmatter task_id '{fm['task_id']}' does not match '{task_id}'")

    # Check step continuity
    if seen_steps and seen_steps != sorted(seen_steps):
        warnings.append("Artifact step numbers are not in ascending order")
    if seen_steps:
        expected = list(range(seen_steps[0], seen_steps[0] + len(seen_steps)))
        if seen_steps != expected:
            warnings.append(f"Step number gaps detected: have {seen_steps}, expected {expected}")

    return _print_validation(task_id, errors, warnings)


def _print_validation(task_id: str, errors: list, warnings: list) -> int:
    """Print validation results and return the exit code: 1 when there
    are errors."""
    if not errors and not warnings:
        print(f"Validate {task_id}: OK")
        return 0

    print(f"Validate {task_id}:")
    for e in errors:
        print(f"  ERROR: {e}")
    for w in warnings:
        print(f"  WARNING: {w}")

    if errors:
        print(f"\n  {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    else:
        print(f"\n  {len(warnings)} warning(s), no errors")
    return 0


def cmd_abort(task_id: str, config: dict) -> int:
    """Abort a task run. Returns the exit code."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        return 1
    state["status"] = "abandoned"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["updated"] = now
    save_run_state(task_id, state, config)
    awake.release(settings.TASKS_DIR / task_id)
    print(f"Task {task_id} aborted.")
    return 0


def cmd_recover(config: dict, task_id: str | None = None, init: bool = False,
                 from_projections: bool = False, apply: bool = False,
                 regenerate_projections: bool = False, decisions_path: str | None = None) -> int:
    """Operator-facing recovery/reconciliation surface for the sqlite
    backend. Deliberately minimal and always explicit -- nothing here runs
    as a side effect of an ordinary command. get_db_connection() (used by
    every ordinary command) raises db.DatabaseUnavailable pointing an
    operator at this command; this command is where that response lives.

    Reuses the existing read-only migrate_legacy.dry_run_migration()/reconcile_task()
    machinery and migrate_legacy.import_task()/db.write_projection() -- no parallel
    reconciliation or projection logic. Returns the exit code.
    """
    if not _sqlite_enabled(config):
        print("persistence_backend is not 'sqlite' -- nothing to recover.")
        return 0

    chosen = [name for name, flag in
              (("--init", init), ("--from-projections", from_projections),
               ("--regenerate-projections", regenerate_projections)) if flag]
    if len(chosen) > 1:
        print(f"Error: {' and '.join(chosen)} are mutually exclusive -- pick one action per run.")
        return 1

    db_path = settings._db_path()
    # Reconciliation validates against the state-machine enums. Ordinary
    # commands register them when they open a connection, but a dry run
    # never opens one, so register them here too (idempotent).
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)

    if regenerate_projections:
        # Regenerating a projection presupposes the canonical database it
        # would be regenerated FROM already exists -- use connect_existing()
        # (fail-closed), never init_database(). Silently creating an empty
        # database here would be exactly the "guess and continue" failure
        # mode this whole recovery surface exists to prevent, just reached
        # via a different flag.
        try:
            conn = db.connect_existing(db_path)
        except db.DatabaseUnavailable as e:
            print(f"Error: {e}")
            return 1
        targets = [task_id] if task_id else [t["task_id"] for t in db.list_tasks(conn)]
        for tid in targets:
            try:
                path = db.write_projection(conn, tid, settings.TASKS_DIR / tid)
                print(f"  Regenerated: {path}")
            except Exception as e:
                print(f"  FAILED to regenerate projection for '{tid}': {e}")
        return 0

    if init:
        if db_path.exists():
            try:
                probe = db.connect_existing(db_path)
                probe.close()
                print(f"Database already exists and is healthy at {db_path}. Nothing to do.")
            except db.DatabaseCorrupt as e:
                print(f"Refusing to touch the existing file at {db_path}: {e}")
                print("  Move it aside manually if you intend to replace it, then re-run --init.")
                return 1
            return 0
        conn = db.init_database(db_path)
        conn.close()
        print(f"Created new canonical database at {db_path}.")
        return 0

    if decisions_path and not from_projections:
        print("Error: --decisions only applies with --from-projections.")
        return 1

    if from_projections and decisions_path:
        return _recover_with_decisions(db_path, Path(decisions_path).expanduser(), apply)

    if from_projections:
        summary = migrate_legacy.dry_run_migration(settings.TASKS_DIR)
        print(f"Scanned {summary['total']} task(s) under {settings.TASKS_DIR}: "
              f"{summary['with_run_yaml']} with run.yaml, "
              f"{summary['without_run_yaml']} with artifacts but no run.yaml")
        print(f"  Clean:   {summary['clean']}")
        print(f"  Flagged: {summary['flagged']}")
        for r in summary["results"]:
            if not r["ok"]:
                print(f"  FLAGGED {r['task_id']}:")
                for d in r["discrepancies"]:
                    print(f"    - {d}")
        if not apply:
            print("\nDry-run only -- nothing written. Re-run with --apply to import clean tasks.")
            return 0
        if not db_path.exists():
            print(f"\nCannot --apply: no database at {db_path}. Run 'relay recover --init' first.")
            return 1
        conn = db.connect_existing(db_path)
        imported, skipped = 0, 0
        for r in summary["results"]:
            tid = r["task_id"]
            if not r["ok"]:
                print(f"  Skipping '{tid}': flagged, needs an explicit migration decision "
                      "(re-run with --decisions FILE).")
                skipped += 1
                continue
            if db.task_exists(conn, tid):
                skipped += 1
                continue
            try:
                migrate_legacy.import_task(conn, tid, settings.TASKS_DIR / tid)
            except (ValueError, sqlite3.Error) as error:
                print(f"  FAILED '{tid}': {error}")
                skipped += 1
                continue
            imported += 1
        print(f"\nImported {imported} task(s), skipped {skipped}. "
              "Each task imports atomically on its own; a failure leaves earlier imports in place.")
        return 0

    print("Usage: relay recover --init | --from-projections [--decisions FILE] [--apply] | --regenerate-projections [task_id]")
    return 0


# Python: Catching specific exception types (FileNotFoundError, ValueError,
#   OSError) lets each failure get its own clear message instead of a
#   stack trace.
def _recover_with_decisions(db_path: Path, decisions_path: Path, apply: bool) -> int:
    """--from-projections with a decisions file: rehearse every import in a
    throwaway in-memory database first, show where each task lands, and
    only with --apply write the clean and decision-resolved tasks.
    Returns the exit code."""
    try:
        decisions = migrate_legacy.load_decisions(decisions_path)
    except FileNotFoundError:
        print(f"Error: decisions file not found: {decisions_path}")
        return 1
    except ValueError as error:
        print(f"Error: invalid decisions file: {error}")
        return 1
    except OSError as error:
        print(f"Error: cannot read decisions file {decisions_path}: {error.strerror}")
        return 1
    rehearsal = migrate_legacy.rehearse_migration(settings.TASKS_DIR, decisions)
    print(f"Rehearsed {rehearsal['scanned']} task(s) under {settings.TASKS_DIR} "
          f"with {len(decisions)} decision(s) from {decisions_path}")
    print(f"  Clean:      {len(rehearsal['clean'])}")
    print(f"  Resolved:   {len(rehearsal['resolved'])}")
    print(f"  Excluded:   {len(rehearsal['excluded'])}")
    print(f"  Unresolved: {len(rehearsal['unresolved'])}")
    for unresolved_task, reason in rehearsal["unresolved"]:
        print(f"  UNRESOLVED {unresolved_task}: {reason}")
    for unknown_task in rehearsal["unknown_decisions"]:
        print(f"  UNKNOWN DECISION {unknown_task}: no such task in the scan")
    if not apply:
        print("\nDry-run only -- nothing written. Re-run with --apply to import "
              "clean and resolved tasks.")
        return 0
    if not db_path.exists():
        print(f"\nCannot --apply: no database at {db_path}. Run 'relay recover --init' first.")
        return 1
    conn = db.connect_existing(db_path)
    imported, already_present, failed = 0, 0, 0
    for importable_task in rehearsal["clean"] + rehearsal["resolved"]:
        if db.task_exists(conn, importable_task):
            already_present += 1
            continue
        try:
            migrate_legacy.import_task(conn, importable_task, settings.TASKS_DIR / importable_task,
                           decision=decisions.get(importable_task))
        except (ValueError, sqlite3.Error) as error:
            print(f"  FAILED '{importable_task}': {error}")
            failed += 1
            continue
        imported += 1
    print(f"\nImported {imported} task(s); {already_present} already present; {failed} failed; "
          f"{len(rehearsal['excluded'])} excluded; {len(rehearsal['unresolved'])} unresolved. "
          "Each task imports atomically on its own.")
    return 0


def cmd_register(repo_path: str, alias: str | None = None) -> int:
    """Register a repository with relay. Returns the exit code."""
    path = Path(repo_path).resolve()
    if not path.exists():
        print(f"Error: Path does not exist: {path}")
        return 1
    if not path.is_dir():
        print(f"Error: Path is not a directory: {path}")
        return 1

    alias = alias or path.name

    # Load or create registry
    registry = {}
    if settings.REGISTRY_PATH and settings.REGISTRY_PATH.exists():
        with open(settings.REGISTRY_PATH) as f:
            registry = load_yaml(f.read()) or {}

    repos = registry.get("repos", {})
    repos[alias] = str(path)
    registry["repos"] = repos

    # Save registry
    if settings.REGISTRY_PATH:
        settings.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(settings.REGISTRY_PATH, "w") as f:
            f.write(dump_yaml(registry))
    else:
        print("Warning: No central install found. Cannot save registry.")
        return 0

    # Create .relay.yaml in target repo if missing
    relay_yaml = path / ".relay.yaml"
    if not relay_yaml.exists():
        with open(relay_yaml, "w") as f:
            f.write(dump_yaml({"alias": alias}))
        print(f"  Created {relay_yaml}")

    print(f"Registered: {alias} -> {path}")
    return 0


def cmd_gate_check(task_id: str, config: dict):
    """Say whether a task is waiting, and on whom (plain-text form)."""
    payload = gate_check_payload(task_id, config)
    gate = payload["gate"]
    print(f"Task: {payload['task_id']}")
    print(f"  Status: {payload['status']}")
    print(f"  Phase: {payload['current_phase']}")
    kind = f" ({gate['kind']})" if gate["kind"] else ""
    print(f"  Gate: {gate['state']}{kind}")
    print(f"  Reason: {gate_card.printable(gate['reason'], REASON_PRINT_LIMIT)}")
    if "fix_rounds" in gate:
        _print_fix_rounds(gate["fix_rounds"])
    if "review_loops" in gate:
        _print_review_loops(gate["review_loops"], task_id, config)
    if gate["kind"] == GATE_ARBITER_ESCALATION and gate["arbiter"]["needs_owner_because"]:
        print(f"  Relay warns: {'; '.join(gate['arbiter']['needs_owner_because'])}.")
        test_run = _escalation_test_run(task_id, gate, config)
        if _names_the_run(gate["arbiter"]["needs_owner_because"], test_run):
            _print_how_testing_changed(test_run, ESCALATION_ACCEPTS)
    if "arbiter" in gate and gate["arbiter"]["recommends"]:
        print(f"  The Arbiter recommends: {gate_card.printable(gate['arbiter']['recommends'])}")
    if gate["next_phase"]:
        print(f"  Next phase: {gate['next_phase']}")
    if gate["send_back_command"]:
        recommended = " (recommended)" if gate["send_back_recommended"] else ""
        print(f"  To send back{recommended}: {gate['send_back_command']}")
    if gate["approve_command"]:
        print(f"  To approve: {gate['approve_command']}")


def _escalation_test_run(task_id: str, gate: dict, config: dict) -> dict | None:
    """Relay's run at the report an escalation's Arbiter ruled on."""
    state = load_run_state(task_id, config) or {}
    trigger = arbiter.trigger_of(state.get("history"), gate["arbiter"]["step"])
    return latest_test_run(task_id, config, trigger.step) if trigger else None


def cmd_profile(alias_or_path: str, as_json: bool) -> int:
    """Check a repo's project profile. Exit code 0 only when it is valid."""
    try:
        payload = profile_payload(alias_or_path)
    except RepoNotFound as e:
        if as_json:
            print_json(error_payload(ERROR_REPO_NOT_FOUND, str(e)))
        else:
            print(f"Error: {e}")
        return 1
    if as_json:
        print_json(payload)
        return 0 if payload["valid"] else 1

    print(f"Profile: {payload['profile_path']}")
    for problem in payload["errors"]:
        print(f"  ERROR: {gate_card.printable(problem, PROFILE_PROBLEM_PRINT_LIMIT)}")
    for concern in payload["warnings"]:
        print(f"  Warning: {gate_card.printable(concern, PROFILE_PROBLEM_PRINT_LIMIT)}")
    if payload["valid"]:
        commands = payload["profile"]["commands"]
        print(f"  Valid. Test command: {commands['test']}")
        targets = [target["name"] for target in payload["profile"].get("deploy_targets") or []]
        print(f"  Deploy targets: {', '.join(targets) if targets else 'none'}")
    elif not payload["found"]:
        print(f"  Start from: {RELAY_HOME / 'templates' / 'profile.yaml'}")
    return 0 if payload["valid"] else 1


JSON_COMMANDS = ("status", "list", "gate-check")


def run_json_command(command: str, task_id: str | None, config: dict) -> int:
    """Print one JSON document for a read command and return the exit code:
    0 with the answer, 1 with a relay.error document. Stdout carries JSON only."""
    try:
        if _sqlite_enabled(config):
            use_read_only_ledger()
        if command == "list":
            payload = list_payload(config)
        elif command == "status":
            payload = status_payload(task_id, config)
        else:
            payload = gate_check_payload(task_id, config)
        document = json.dumps(payload, indent=2, default=str)
    except TaskNotFound as e:
        print_json(error_payload(ERROR_TASK_NOT_FOUND, str(e)))
        return 1
    except db.DatabaseUnavailable as e:
        # Fail closed: no ledger, no answer. Never fall back to projections.
        print_json(error_payload(ERROR_LEDGER_UNAVAILABLE, str(e)))
        return 1
    # Python: catching Exception is usually too broad. Here it is the point:
    #   whatever went wrong, a front end still gets a JSON document it can
    #   read as "no answer", never a traceback and an empty stdout.
    except Exception as e:
        print_json(error_payload(ERROR_INTERNAL, f"{type(e).__name__}: {e}"))
        return 1
    print(document)
    return 0
