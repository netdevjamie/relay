"""The commands behind the `relay` CLI: start, next, status, list,
override, send-back, note, validate, abort, recover,
register, gate-check, profile, and the phone's waiting, card, show and
decide.
"""

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import awake
import board_view
import db
import evidence
import proto
import settings
import sizing
import agents
import gate_card
import read_api
from settings import (
    FIRST_PHASE, PIPELINE_ORDER, RELAY_HOME, VALID_PHASES, VALID_STATUSES,
    VALID_VERDICTS, _CLI_CMD, _IS_LEGACY, _LEGACY_WORKFLOW_DIR, _resolve_repo_path, _task_cwd,
    _sqlite_enabled, dump_yaml, get_db_connection, load_yaml, use_read_only_ledger,
)
from run_state import (
    _is_agent_artifact, _record_verdict_in_history, check_state_consistency,
    find_latest_artifact, get_next_step, increment_retry, init_run_state,
    load_run_state, parse_frontmatter, record_phase, reset_retries, save_run_state,
)
from routing import (
    GATE_APPROVAL, GATE_BLOCKED, GATE_DEPLOY_APPROVAL, GATE_DEPLOY_FLAG_MISSING,
    GATE_FIX_ROUNDS, GATE_RETRY_LIMIT, ROUTE_COMPLETE, ROUTE_COMPLETE_SKIPPING_OPS, ROUTE_OWNER_GATE,
    ROUTE_RETRY_LIMIT, SEND_BACK_GATE_KINDS, _deploy_flag, decide_routing, determine_next,
    fix_round_summary, send_back_target,
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
# line (the longest seen was 233 characters); a long name
# from the profile is cut.
PROFILE_PROBLEM_PRINT_LIMIT = 400


def _accept_test_command(task_id: str, state: dict, to_phase: str, config: dict) -> None:
    """Called by `relay override`: if the task is waiting for the owner to
    accept a changed test command and the override moves it forward to the
    phase that gate offered, record the acceptance. An override to anywhere
    else (back to the Builder, say) accepts nothing."""
    latest_step = read_latest_step(task_id, state, config)
    if not latest_step or latest_step["verdict"] is None:
        return
    phase, verdict = latest_step["phase"], latest_step["verdict"]
    test_run = latest_test_run(task_id, config, latest_step["step"])
    if not _test_command_awaits_owner(task_id, config, phase, verdict, test_run):
        return
    if to_phase != determine_next(phase, verdict, config)[0]:
        return
    db.record_test_command_accepted(get_db_connection(), task_id, phase=phase,
                                    step=latest_step["step"], command=test_run["command"],
                                    test_report=test_run.get("test_report"),
                                    commit=test_run.get("commit"))
    print(f"Accepted this task's test command: {test_run['command']}")


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


def _ref_exists(repo_path: Path, ref: str) -> bool:
    return _git(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") is not None


def _default_base(repo_path: Path) -> str:
    """The repo's default branch, as the ref a task is based on:
    origin's HEAD when it is known, else the first of origin/main,
    origin/master, main and master that exists."""
    origin_head = _git(repo_path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if origin_head:
        return origin_head.replace("refs/remotes/", "", 1)
    for candidate in ("origin/main", "origin/master", "main", "master"):
        if _ref_exists(repo_path, candidate):
            return candidate
    raise CheckoutError(f"could not find the default branch of {repo_path}; give --base")


def _resolve_base(repo_path: Path, base: str | None) -> str:
    """The base ref: --base as given or on origin, else the default branch."""
    if base is None:
        return _default_base(repo_path)
    for candidate in (base, f"origin/{base}"):
        if _ref_exists(repo_path, candidate):
            return candidate
    raise CheckoutError(f"base branch '{base}' not found in {repo_path} "
                        f"(tried '{base}' and 'origin/{base}'). Fetch it or check the name.")


def _common_dir(path) -> str | None:
    """The real path of the git folder a checkout and its worktrees share."""
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(common) if common else None


def task_checkout(repo_path: Path, worktree: str | None, base: str | None) -> dict:
    """{"worktree_path", "branch", "base"} for the checkout a task builds in:
    `worktree`, or the current folder. It must be a checkout of the
    registered repo (the repo itself or one of its worktrees), on a branch
    of its own: not a detached HEAD, and not the default branch."""
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
    resolved_base = _resolve_base(repo_path, base)
    default = _default_base(repo_path)
    if branch in (default, default.removeprefix("origin/"), resolved_base,
                  resolved_base.removeprefix("origin/")):
        raise CheckoutError(f"{top} is on '{branch}', the branch the task is based on; "
                            f"make a branch for the task first (git switch -c <name>)")
    return {"worktree_path": os.path.realpath(top), "branch": branch, "base": resolved_base}


def cmd_start(task_id: str, auto: bool, config: dict, deploy_intent: bool = False,
              repo: str | None = None, base: str | None = None, worktree: str | None = None):
    """Start a new task run. With a registered git repo, the task builds in
    `worktree` (default: the current folder), which must be a checkout of
    that repo on a branch of its own; Relay records it and makes none."""
    # Validate the task id up front: it becomes a path segment.
    try:
        task_id = sanitize_task_id(task_id)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Look for brief in task dir (central) or legacy briefs/ dir
    task_dir = settings.TASKS_DIR / task_id
    brief = task_dir / "brief.md"
    if not brief.exists() and _IS_LEGACY:
        brief = _LEGACY_WORKFLOW_DIR / "briefs" / f"{task_id}.md"
    if not brief.exists():
        print(f"Error: No task brief found.")
        print(f"  Central: {task_dir}/brief.md")
        if _IS_LEGACY:
            print(f"  Legacy:  {_LEGACY_WORKFLOW_DIR / 'briefs' / (task_id + '.md')}")
        sys.exit(1)

    if load_run_state(task_id, config) is not None:
        print(f"Error: Task {task_id} already has a run. Use 'next' to advance or 'abort' to restart.")
        sys.exit(1)

    if repo and not _IS_LEGACY and _resolve_repo_path(repo) is None:
        print(f"Error: '{repo}' is not a registered repo.")
        print(f"  Register it first: relay register <path> --alias {repo}")
        sys.exit(1)

    task_dir.mkdir(parents=True, exist_ok=True)

    task_sizing = sizing.read_brief_sizing(task_id)
    if task_sizing.deploy is not None:
        deploy_intent = deploy_intent or task_sizing.deploy

    state = init_run_state(task_id)
    state["deploy_intent"] = deploy_intent
    state["repo"] = repo

    # Record the checkout the task builds in, when the repo is a registered
    # git repo (central mode). Legacy/in-place tasks keep running in place.
    repo_path = _resolve_repo_path(repo) if repo else None
    if repo_path and (repo_path / ".git").exists() and not _IS_LEGACY:
        try:
            checkout = task_checkout(repo_path, worktree, base)
        except CheckoutError as e:
            print(f"Error: {e}.")
            print(f"  Run it from the task's checkout, or give --worktree PATH.")
            sys.exit(1)
        if _sqlite_enabled(config):
            sharing = db.open_tasks_in(get_db_connection(), checkout["worktree_path"])
            if sharing:
                print(f"Error: {', '.join(sharing)} still builds in {checkout['worktree_path']}; "
                      "two tasks in one checkout would test each other's changes.")
                print(f"  Give this task its own worktree (git worktree add -b <branch> <path>).")
                sys.exit(1)
        state.update(checkout)
    elif worktree:
        print(f"Error: --worktree needs --repo with a registered git repo.")
        sys.exit(1)
    elif base:
        print(f"Warning: --base '{base}' ignored (no checkout recorded for this task).")

    save_run_state(task_id, state, config)
    sizing.record(task_id, task_sizing, config)

    step = 1
    first = sizing.first_phase(task_sizing)
    state["current_phase"] = first
    name = artifact_name(first, step, state)
    prompt = build_prompt(first, task_id, step, name, state=state)

    print(f"Task {task_id} started.")
    print(f"  Phase: {first}")
    print(f"  {sizing.summary(task_sizing)}")
    print(f"  Artifact: {name}")
    print(f"  Deploy intent: {deploy_intent}")
    if state.get("worktree_path"):
        print(f"  Worktree: {state['worktree_path']}")
        print(f"  Branch:   {state['branch']} (base {state['base']})")

    state["next_step"] = step + 1
    record_phase(state, first, step)
    save_run_state(task_id, state, config)

    agents.invoke_agent(first, task_id, prompt, config, auto, cwd=_task_cwd(state))


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
          f"{phase} (step {step}) being dispatched and its artifact being written. "
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
    stopped. Sending back is recommended where the tripwire stopped the
    same route: the latest report still holds a high."""
    recommended = "recommended; " if send_back_recommended else ""
    print(f"  Read the latest report, then choose:")
    print(f"    {_CLI_CMD} send-back {task_id} --note \"what to fix\"   ({recommended}one more round; the count starts again)")
    print(f"    {_CLI_CMD} override {task_id} <phase>                 (move it somewhere else)")
    print(f"    {_CLI_CMD} abort {task_id}")


def _print_fix_rounds(listed: dict) -> None:
    """The tripwire's list: the sentence that opens it, then a line per round."""
    opening, *rounds = listed["lines"]
    print(f"  {opening}")
    _print_lines(rounds, indent="    ")


def _print_fix_round_gate(task_id: str, artifact: Path, listed: dict) -> None:
    print(f"\n  HUMAN GATE: fix rounds are still finding highs.")
    print(f"  Review the artifact at: {artifact}")
    _print_fix_rounds(listed)
    print(f"\n  To send it back to the builder for fixes (recommended):")
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


def cmd_next(task_id: str, auto: bool, config: dict, force_phase: str | None = None):
    """Parse the latest artifact and advance the workflow."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"Error: No run state for {task_id}. Use 'start' first.")
        sys.exit(1)

    if state.get("status") in ("completed", "abandoned"):
        print(f"Task {task_id} is already {state['status']}.")
        sys.exit(0)

    # A blocked task (paused, or stopped at the limit on automatic rounds)
    # moves again only when the owner says so. Refuse before recording anything.
    if state.get("status") == "blocked":
        gate = read_api.read_gate(task_id, state, read_latest_step(task_id, state, config), config)
        if gate["kind"] == GATE_RETRY_LIMIT:
            print(f"Task {task_id} is stopped: {_limit_stop_words(gate['next_phase'], state, config)}")
            if "fix_rounds" in gate:
                _print_fix_rounds(gate["fix_rounds"])
            _print_limit_choices(task_id, gate["send_back_recommended"])
        else:
            _print_paused(task_id)
            print(f"  It stopped in phase: {state.get('current_phase')}")
        _stop_for_owner(task_id, config)
        sys.exit(1)

    # Find and parse latest artifact
    latest_path, frontmatter = find_latest_artifact(task_id, config)
    if not latest_path:
        print(f"No artifacts found for {task_id}. Run the first agent.")
        sys.exit(1)

    verdict = frontmatter.get("verdict")
    phase = frontmatter.get("phase", state.get("current_phase"))

    if not verdict:
        valid = VALID_VERDICTS.get(phase, [])
        print(f"Warning: No verdict found in {latest_path.name}")
        print(f"  The artifact may be missing YAML frontmatter.")
        print(f"  Current phase: {phase}")
        print(f"  Expected one of: {', '.join(valid)}")
        print(f"\n  Options:")
        print(f"    1. Fix the artifact and run 'next' again")
        print(f"    2. Use 'override {task_id} <phase>' to manually advance")
        sys.exit(1)

    # The phase has finished: let go of its hold, and say so if the machine
    # slept while the step was open.
    awake.release(settings.TASKS_DIR / task_id)
    _note_sleep_during_step(task_id, state, phase, frontmatter.get("step"), latest_path, config)

    # The Architect decides whether the task deploys; its plan's `deploy`
    # flag overrides the --deploy guess given at start. Read it from the
    # artifact file itself: the sqlite backend's step record keeps only
    # phase and verdict.
    planned_deploy = None
    if phase == "architect":
        planned_deploy = _deploy_flag(parse_frontmatter(latest_path).get("deploy"))
        previous_deploy = bool(state.get("deploy_intent"))
        if planned_deploy is not None and planned_deploy != previous_deploy:
            print(f"  Architect set deploy: {'yes' if planned_deploy else 'no'}")
            state["deploy_intent"] = planned_deploy
            if _sqlite_enabled(config):
                db.record_deploy_decision(get_db_connection(), task_id,
                                          step=frontmatter.get("step"),
                                          previous=previous_deploy, decided=planned_deploy)

    _record_verdict_in_history(state, phase, verdict)
    save_run_state(task_id, state, config)

    print(f"Task: {task_id}")
    print(f"  Latest artifact: {latest_path.name}")
    print(f"  Phase: {phase}")
    print(f"  Verdict: {verdict}")

    # Check for state contradictions (warn but don't block)
    artifact_step = frontmatter.get("step", 0)
    if isinstance(artifact_step, str):
        try:
            artifact_step = int(artifact_step)
        except ValueError:
            artifact_step = 0
    check_state_consistency(task_id, state, phase, artifact_step, config)

    test_run = ensure_test_run(task_id, state, phase, verdict, frontmatter.get("step"), config)
    if test_run:
        print(f"  {evidence.summary(test_run)}")

    decision = decide_routing(state, phase, verdict, config, planned_deploy=planned_deploy,
                              force_phase=force_phase, test_run=test_run,
                              test_command_confirmed=is_test_command_confirmed(task_id, config, test_run),
                              sizing=sizing.read_sizing(task_id, config))
    next_phase = decision.next_phase

    print(f"  Routing: {decision.reason}")

    # Workflow complete
    if decision.outcome == ROUTE_COMPLETE:
        _close(state)
        save_run_state(task_id, state, config)
        print(f"\n  Workflow complete for {task_id}.")
        return

    if decision.redirected_to_tester:
        print(f"\n  No Tester step since the last build (task predates the new order). "
              f"Running the Tester next.")

    # Ops runs only for deploy tasks
    if decision.outcome == ROUTE_COMPLETE_SKIPPING_OPS:
        print(f"\n  No deploy intent set. Skipping ops.")
        print(f"  To run ops anyway: relay override {task_id} ops")
        _close(state)
        save_run_state(task_id, state, config)
        print(f"  Workflow complete for {task_id}.")
        return

    if decision.outcome == ROUTE_OWNER_GATE:
        _print_owner_gate(task_id, state, decision, phase, verdict, latest_path, test_run, config)
        _stop_for_owner(task_id, config)
        return
    if decision.outcome == ROUTE_RETRY_LIMIT:
        _print_owner_gate(task_id, state, decision, phase, verdict, latest_path, test_run, config)
        state["status"] = "blocked"
        save_run_state(task_id, state, config)
        _stop_for_owner(task_id, config)
        return
    _dispatch_next(task_id, state, next_phase, test_run=test_run,
                   counts_as_retry=decision.counts_as_retry, config=config, auto=auto)


def _print_owner_gate(task_id: str, state: dict, decision, phase: str, verdict: str,
                      latest_path: Path, test_run: dict | None, config: dict) -> None:
    """What `relay next` prints where the decision stops for the owner: a
    gate, or the stop at the limit on automatic rounds. It prints only; the
    caller saves whatever the stop changes."""
    next_phase = decision.next_phase
    if decision.outcome == ROUTE_RETRY_LIMIT:
        print(f"\n  STOPPED: {_limit_stop_words(next_phase, state, config)}")
        if decision.lists_fix_rounds:
            _print_fix_rounds(fix_round_summary(state))
        _print_limit_choices(task_id, decision.send_back_recommended)
        return
    if decision.gate_kind == GATE_DEPLOY_FLAG_MISSING:
        print(f"\n  HUMAN GATE: the plan doesn't say whether this task deploys.")
        print(f"  Add 'deploy: yes' or 'deploy: no' to the frontmatter of")
        print(f"    {settings.TASKS_DIR / task_id / latest_path.name}")
        print(f"  then run: {_CLI_CMD} next {task_id}")
        print(f"  (Overriding past this keeps the start-time guess: deploy={'yes' if state.get('deploy_intent') else 'no'}.)")
        return
    if decision.gate_kind == GATE_FIX_ROUNDS:
        _print_fix_round_gate(task_id, settings.TASKS_DIR / task_id / latest_path.name,
                              fix_round_summary(state))
        return
    print(f"\n  HUMAN GATE: Approval required.")
    print(f"  Review the artifact at: {settings.TASKS_DIR / task_id / latest_path.name}")
    if _test_command_awaits_owner(task_id, config, phase, verdict, test_run):
        print(f"\n  This task changed how the repo is tested. The passing run used:")
        print(f"    {test_run['command']}")
        print(f"  The base branch's test command: {test_run.get('base_command') or '(none)'}")
        if test_run.get("base_profile_unread"):
            why = gate_card.printable(test_run["base_profile_unread"], PROFILE_PROBLEM_PRINT_LIMIT)
            print(f"  {evidence.BASE_PROFILE_UNREAD}: {why}")
        print(f"  Approving accepts the new command for the rest of this task.")
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
              f"(back to {_role_in_words(send_back_target(phase))})")
        print(f"    {_CLI_CMD} override {task_id} <phase>")
        print(f"    {_CLI_CMD} abort {task_id}")


def _dispatch_next(task_id: str, state: dict, next_phase: str, *, test_run: dict | None,
                   counts_as_retry: bool, config: dict, auto: bool) -> None:
    """Start the phase routing chose: count the round when nobody chose it,
    record the step and run its agent."""
    if counts_as_retry:
        increment_retry(state, next_phase)

    # Advance
    step = get_next_step(task_id, config)
    name = artifact_name(next_phase, step, state)
    prompt = build_prompt(next_phase, task_id, step, name, state=state)
    prompt += _test_run_prompt(task_id, test_run, next_phase)
    prompt += owner_notes_prompt(task_id, config)

    state["current_phase"] = next_phase
    state["next_step"] = step + 1
    record_phase(state, next_phase, step, verdict=None)
    save_run_state(task_id, state, config)

    print(f"\n  Next phase: {next_phase}")
    print(f"  Artifact: {name}")

    agents.invoke_agent(next_phase, task_id, prompt, config, auto, cwd=_task_cwd(state))


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
                v = f" -> {entry['verdict']}" if entry.get("verdict") else ""
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

    test_runs = read_test_runs(task_id, config)
    if test_runs:
        print(f"\n  Test runs (Relay's own):")
        for run in test_runs:
            print(f"    after step {run['step']} ({run['phase']}): {evidence.summary(run)}")

    # Show artifacts
    task_dir = settings.TASKS_DIR / task_id
    if task_dir.exists():
        artifacts = sorted(f.name for f in task_dir.iterdir() if _is_agent_artifact(f))
        if artifacts:
            print(f"\n  Artifacts:")
            for a in artifacts:
                fm = parse_frontmatter(task_dir / a)
                verdict = fm.get("verdict", "")
                v_str = f" [{verdict}]" if verdict else ""
                print(f"    {a}{v_str}")


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
                 note: str | None = None):
    """Manually set the next phase (human override). `note` is what the owner
    says while doing it: recorded with the approval and given to later agents."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        sys.exit(1)

    allowed = PIPELINE_ORDER + ["done"]  # legacy phases are readable, not runnable
    if phase not in allowed:
        print(f"Invalid phase: {phase}. Must be one of: {', '.join(allowed)}")
        sys.exit(1)
    note = _note_or_exit(note)

    # Overriding out of a stop at the limit is the owner's decision: the
    # stopped phase's count starts again wherever the task goes.
    latest_step = read_latest_step(task_id, state, config)
    gate = read_api.read_gate(task_id, state, latest_step, config)
    stopped_phase = gate["next_phase"] if gate["kind"] == GATE_RETRY_LIMIT else None
    _override_into(task_id, state, phase, note, stopped_phase, config, auto)


def _override_into(task_id: str, state: dict, phase: str, note: str | None,
                   stopped_phase: str | None, config: dict, auto: bool) -> None:
    """The override itself: record it, start the counts the owner decided
    again, and start the phase (or close the task)."""
    if _sqlite_enabled(config):
        _accept_test_command(task_id, state, phase, config)
        db.record_approval(get_db_connection(), task_id, phase=state.get("current_phase"),
                            next_phase=phase, method="override", note=note,
                            stopped_phase=stopped_phase)
    elif note:
        print("  Warning: the files backend keeps no events, so this note is not recorded.")
    if stopped_phase:
        reset_retries(state, stopped_phase)

    if phase == "done":
        awake.release(settings.TASKS_DIR / task_id)
        _close(state)
        save_run_state(task_id, state, config)
        print(f"Task {task_id} marked as completed.")
        return

    reset_retries(state, phase)  # the owner chose this round
    step = get_next_step(task_id, config)
    name = artifact_name(phase, step, state)
    prompt = build_prompt(phase, task_id, step, name, state=state)
    test_runs = read_test_runs(task_id, config)
    if test_runs:
        prompt += _test_run_prompt(task_id, test_runs[-1], phase)
    prompt += owner_notes.prompt_with(task_id, config, note)

    state["status"] = "in_progress"
    state["current_phase"] = phase
    state["next_step"] = step + 1
    record_phase(state, phase, step)
    save_run_state(task_id, state, config)

    print(f"Moving to {phase}.")
    print(f"  Artifact: {name}")

    agents.invoke_agent(phase, task_id, prompt, config, auto, cwd=_task_cwd(state))


def cmd_send_back(task_id: str, config: dict, auto: bool = False, note: str | None = None):
    """Send the latest finished step back for more work (the owner's answer
    to a gate when the work is not good enough yet). Not an approval. It is
    the owner's decision, so the limit on automatic rounds never refuses it
    and the count for the phase it goes to starts again.

    Only allowed where gate-check offers it: the task is waiting on the
    owner at an approval or owner-decision gate, or the limit stopped it.
    Anywhere else it refuses and records nothing."""
    note = _note_or_exit(note)
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        sys.exit(1)
    if state.get("status") in ("completed", "abandoned"):
        print(f"Task {task_id} is already {state['status']}.")
        sys.exit(1)

    latest_step = read_latest_step(task_id, state, config)
    gate = read_api.read_gate(task_id, state, latest_step, config)
    if gate["state"] == GATE_WAITING_ON_AGENT:
        phase = latest_step["phase"] if latest_step else state.get("current_phase")
        print(f"Nothing to send back: {phase} has not finished its artifact yet.")
        sys.exit(1)
    if gate["state"] == GATE_READY:
        print(f"Nothing is waiting on you: '{_CLI_CMD} next {task_id}' will move on ({gate['reason']}).")
        print(f"  To redo a phase anyway: {_CLI_CMD} override {task_id} <phase>")
        sys.exit(1)
    kind = gate["kind"]
    if kind == GATE_BLOCKED:
        _print_paused(task_id)
        sys.exit(1)
    if kind not in SEND_BACK_GATE_KINDS or not gate["send_back_command"]:
        print(f"send-back does not apply at this gate ({gate['kind']}): {gate['reason']}")
        sys.exit(1)

    phase = latest_step["phase"]
    # At the limit stop the round goes to the phase that was stopped.
    rework_phase = gate["next_phase"] if kind == GATE_RETRY_LIMIT else send_back_target(phase)
    _send_back(task_id, state, latest_step, rework_phase, note, config, auto)


def _send_back(task_id: str, state: dict, latest_step: dict, rework_phase: str,
               note: str | None, config: dict, auto: bool) -> None:
    """The send-back itself: record it, start the phase's count again and
    run the phase that reworks the step."""
    phase, verdict = latest_step["phase"], latest_step["verdict"]
    _record_verdict_in_history(state, phase, verdict)
    reset_retries(state, rework_phase)
    state["status"] = "in_progress"

    step = get_next_step(task_id, config)
    name = artifact_name(rework_phase, step, state)
    prompt = build_prompt(rework_phase, task_id, step, name, state=state)
    prompt += (f"\n\nThis step was sent back by the owner. Answer what "
               f"{latest_step['artifact_path']} says ({phase} verdict: {verdict}): "
               f"fix what it raises, and do not redo work it did not question.")

    state["current_phase"] = rework_phase
    state["next_step"] = step + 1
    record_phase(state, rework_phase, step)
    # The new step is saved first. Once it is saved the task is waiting on
    # the agent, so running send-back again is refused and one decision can
    # never be recorded twice.
    save_run_state(task_id, state, config)
    _record_sent_back(task_id, config, phase, verdict, rework_phase, note)
    prompt += owner_notes.prompt_for_send_back(task_id, config, note)

    print(f"Sent back to {rework_phase}. Automatic rounds: 0 of {config.get('max_retries', 3)}.")
    if not note and _sqlite_enabled(config):  # the files backend has nowhere to keep a note
        print(f"No note recorded. Add one with: {_CLI_CMD} note {task_id} --note \"...\"")
    print(f"  Artifact: {name}")

    agents.invoke_agent(rework_phase, task_id, prompt, config, auto, cwd=_task_cwd(state))


def _record_sent_back(task_id: str, config: dict, phase: str, verdict: str, rework_phase: str,
                      note: str | None) -> None:
    """The ledger's record of a send-back. The older files backend keeps no
    events of any kind (approvals included), so there is nothing to write."""
    if _sqlite_enabled(config):
        db.record_sent_back(get_db_connection(), task_id, from_phase=phase, verdict=verdict,
                            to_phase=rework_phase, note=note)


def _note_or_exit(note: str | None) -> str | None:
    """The owner's note, trimmed, or None when there is none. A note too
    long to keep stops the command before anything is recorded or moved."""
    note, problem = owner_notes.checked(note)
    if problem:
        print(problem)
        sys.exit(1)
    return note


def cmd_note(task_id: str, config: dict, note: str | None):
    """Record something the owner wants on the task's record without moving
    the task: a decision, a correction, a fact. Every later agent is given
    it. Allowed at any time, on any task that has been started."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        sys.exit(1)
    note = _note_or_exit(note)
    if not note:
        print(f"Nothing to record. Usage: {_CLI_CMD} note {task_id} --note \"what you decided\"")
        print(f"  A longer note can come from a file: {_CLI_CMD} note {task_id} --note-file notes.txt")
        sys.exit(1)
    if not _sqlite_enabled(config):
        print("The files backend keeps no events, so a note cannot be recorded.")
        sys.exit(1)
    latest_step = read_latest_step(task_id, state, config)
    db.record_owner_note(get_db_connection(), task_id, phase=state.get("current_phase"),
                         step=latest_step["step"] if latest_step else None, note=note)
    count = len(read_owner_notes(task_id, config))
    if state.get("status") in ("completed", "abandoned"):
        print(f"Note recorded on {task_id} ({count} from the owner so far). The task is "
              f"{state['status']}, so no agent will be given it; it is on the record.")
        return
    print(f"Note recorded on {task_id} ({count} from the owner so far). Later agents will be given it.")
    print(f"  An agent that is already running will not see it; the next one will.")


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


def cmd_show(task_id: str, name: str, config: dict):
    _read_only(config)
    text, problem = gate_card.report_text(task_id, name, config)
    if problem:
        print(problem)
        sys.exit(1)
    _print_lines(gate_card.as_code_block(text.splitlines()))


def _refuse(task_id: str, reason: str, config: dict):
    """Nothing is recorded: say why, then show the card as it is now."""
    print(f"Not applied: {reason}")
    _print_lines(gate_card.card_lines(task_id, config, datetime.now()))
    sys.exit(1)


def _offered_choice(task_id: str, status: dict, choice_name: str, step: int, gate_kind: str,
                    to_phase: str | None, note: str | None, config: dict) -> gate_card.Choice:
    """The choice, if the task still waits on the owner at that step, at
    that kind of gate, and the card there offers it. Anything else is
    refused before anything is recorded, so a decision for a gate that has
    moved on is never applied. A note leaves the task at its gate, so the
    same note sent twice is recorded twice."""
    latest = status["latest_step"] or {}
    now_words = board_view.row_line(gate_card.row_of(status), datetime.now())
    if status["gate"]["state"] != read_api.GATE_WAITING_ON_OWNER or latest.get("step") != step:
        _refuse(task_id, f"{task_id} is no longer waiting at step {step}; it is now {now_words}.",
                config)
    if status["gate"]["kind"] != gate_kind:
        _refuse(task_id, f"{task_id} is no longer at the {gate_card.printable(gate_kind)} gate "
                         f"of step {step}; it is now {now_words}.", config)
    choice = next((offered for offered in gate_card.choices(status) if offered.name == choice_name),
                  None)
    if choice is None:
        _refuse(task_id, f"{gate_card.printable(choice_name)} is not a choice at step {step} "
                         f"of {task_id}.", config)
    if choice.name == "move-to" and to_phase not in choice.targets:
        _refuse(task_id, f"move-to needs --to with one of: {', '.join(choice.targets)}.", config)
    if choice.name != "move-to" and to_phase:
        _refuse(task_id, "--to is for move-to.", config)
    if choice.action == gate_card.NOTE and not (note or "").strip():
        _refuse(task_id, "note needs --note or --note-file.", config)
    return choice


def _apply(choice: gate_card.Choice, task_id: str, to_phase: str | None, note: str | None,
           config: dict, auto: bool) -> None:
    """The CLI command the choice is, called exactly as `relay` calls it.
    Abort takes no note, so a note given with it is recorded first, by
    `relay note`."""
    if note and choice.action == gate_card.ABORT:
        cmd_note(task_id, config, note)
    if choice.action == gate_card.OVERRIDE:
        cmd_override(task_id, to_phase or choice.targets[0], config, auto, note=note)
    elif choice.action == gate_card.SEND_BACK:
        cmd_send_back(task_id, config, auto, note=note)
    elif choice.action == gate_card.ABORT:
        cmd_abort(task_id, config)
    else:
        cmd_note(task_id, config, note)


@contextmanager
def _one_decision_at_a_time(task_id: str, config: dict):
    """Hold the task's decision lock while one decide reads its gate,
    checks the choice and applies it. A second decide on the task in that
    time is refused before it reads the gate or records anything, so two decisions
    sent at the same instant cannot both pass the check. The lock is in the
    `locks` folder beside the tasks folder (Relay's home), one file per task.
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
            _refuse(task_id, f"another decision on {task_id} is under way. Read the card again "
                             f"once it is done.", config)
        yield


def cmd_decide(task_id: str, choice_name: str, step: int, gate_kind: str, to_phase: str | None,
               note: str | None, config: dict, auto: bool = False):
    """Make one choice from a task's card, by its name, for the step and
    the kind of gate the card was for. It runs the same command the owner would type at the Mac,
    so the ledger gets the same events and Relay's checks are the same;
    then it prints back what was recorded. One decide at a time per task."""
    with _one_decision_at_a_time(task_id, config):
        status = read_api.status_payload(task_id, config)
        choice = _offered_choice(task_id, status, choice_name, step, gate_kind, to_phase, note,
                                 config)
        notes_before = len(read_owner_notes(task_id, config))
        try:
            _apply(choice, task_id, to_phase, note, config, auto)
        except SystemExit as stopped:
            if stopped.code not in (0, None):   # Relay's own refusal, already printed
                _print_lines(gate_card.card_lines(task_id, config, datetime.now()))
            raise
        notes = read_owner_notes(task_id, config)
        _print_lines(gate_card.wrapped(_recorded_lines(task_id, step, choice, to_phase, config)))
        print(_recorded_note(notes[-1]["note"] if len(notes) > notes_before else None))


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


def cmd_validate(task_id: str, config: dict):
    """Validate a task's brief, run state, and all artifacts."""
    errors = []
    warnings = []

    # Check brief exists (central or legacy location)
    brief = settings.TASKS_DIR / task_id / "brief.md"
    if not brief.exists() and _IS_LEGACY:
        brief = _LEGACY_WORKFLOW_DIR / "briefs" / f"{task_id}.md"
    if not brief.exists():
        errors.append(f"No task brief found for {task_id}")
    else:
        # Check brief is not empty template
        content = brief.read_text()
        if "{title}" in content or "{YYYYMMDD" in content:
            warnings.append("Task brief appears to still contain template placeholders")

    # Check task dir exists
    task_dir = settings.TASKS_DIR / task_id
    if not task_dir.exists():
        errors.append(f"No handoff folder at {task_dir}")
        _print_validation(task_id, errors, warnings)
        return

    # Check run state (run.yaml under the files backend, the tasks table under sqlite)
    state = load_run_state(task_id, config)
    if not state:
        errors.append("No run.yaml found" if not _sqlite_enabled(config) else "No task row found in database")
        _print_validation(task_id, errors, warnings)
        return

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

        # Parse frontmatter
        fm = parse_frontmatter(art)
        if not fm:
            errors.append(f"{art.name}: missing YAML frontmatter")
            continue

        # Check required frontmatter fields
        for field in ["task_id", "phase", "step", "date", "verdict"]:
            if field not in fm:
                errors.append(f"{art.name}: frontmatter missing '{field}'")

        # Validate phase
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

        # Check task_id matches
        if fm.get("task_id") and fm["task_id"] != task_id:
            errors.append(f"{art.name}: frontmatter task_id '{fm['task_id']}' does not match '{task_id}'")

    # Check step continuity
    if seen_steps and seen_steps != sorted(seen_steps):
        warnings.append("Artifact step numbers are not in ascending order")
    if seen_steps:
        expected = list(range(seen_steps[0], seen_steps[0] + len(seen_steps)))
        if seen_steps != expected:
            warnings.append(f"Step number gaps detected: have {seen_steps}, expected {expected}")

    _print_validation(task_id, errors, warnings)


def _print_validation(task_id: str, errors: list, warnings: list):
    """Print validation results."""
    if not errors and not warnings:
        print(f"Validate {task_id}: OK")
        return

    print(f"Validate {task_id}:")
    for e in errors:
        print(f"  ERROR: {e}")
    for w in warnings:
        print(f"  WARNING: {w}")

    if errors:
        print(f"\n  {len(errors)} error(s), {len(warnings)} warning(s)")
        sys.exit(1)
    else:
        print(f"\n  {len(warnings)} warning(s), no errors")


def cmd_abort(task_id: str, config: dict):
    """Abort a task run."""
    state = load_run_state(task_id, config)
    if not state:
        print(f"No run state for {task_id}.")
        sys.exit(1)
    state["status"] = "abandoned"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["updated"] = now
    save_run_state(task_id, state, config)
    awake.release(settings.TASKS_DIR / task_id)
    print(f"Task {task_id} aborted.")


def cmd_recover(config: dict, task_id: str | None = None, init: bool = False,
                 from_projections: bool = False, apply: bool = False,
                 regenerate_projections: bool = False, decisions_path: str | None = None):
    """Operator-facing recovery/reconciliation surface for the sqlite
    backend. Deliberately minimal and always explicit -- nothing here runs
    as a side effect of an ordinary command. get_db_connection() (used by
    every ordinary command) raises db.DatabaseUnavailable pointing an
    operator at this command; this command is where that response lives.

    Reuses the existing read-only db.dry_run_migration()/reconcile_task()
    machinery and db.import_task()/db.write_projection() -- no parallel
    reconciliation or projection logic.
    """
    if not _sqlite_enabled(config):
        print("persistence_backend is not 'sqlite' -- nothing to recover.")
        return

    chosen = [name for name, flag in
              (("--init", init), ("--from-projections", from_projections),
               ("--regenerate-projections", regenerate_projections)) if flag]
    if len(chosen) > 1:
        print(f"Error: {' and '.join(chosen)} are mutually exclusive -- pick one action per run.")
        sys.exit(1)

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
            sys.exit(1)
        targets = [task_id] if task_id else [t["task_id"] for t in db.list_tasks(conn)]
        for tid in targets:
            try:
                path = db.write_projection(conn, tid, settings.TASKS_DIR / tid)
                print(f"  Regenerated: {path}")
            except Exception as e:
                print(f"  FAILED to regenerate projection for '{tid}': {e}")
        return

    if init:
        if db_path.exists():
            try:
                probe = db.connect_existing(db_path)
                probe.close()
                print(f"Database already exists and is healthy at {db_path}. Nothing to do.")
            except db.DatabaseCorrupt as e:
                print(f"Refusing to touch the existing file at {db_path}: {e}")
                print("  Move it aside manually if you intend to replace it, then re-run --init.")
                sys.exit(1)
            return
        conn = db.init_database(db_path)
        conn.close()
        print(f"Created new canonical database at {db_path}.")
        return

    if decisions_path and not from_projections:
        print("Error: --decisions only applies with --from-projections.")
        sys.exit(1)

    if from_projections and decisions_path:
        _recover_with_decisions(db_path, Path(decisions_path).expanduser(), apply)
        return

    if from_projections:
        summary = db.dry_run_migration(settings.TASKS_DIR)
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
            return
        if not db_path.exists():
            print(f"\nCannot --apply: no database at {db_path}. Run 'relay recover --init' first.")
            sys.exit(1)
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
                db.import_task(conn, tid, settings.TASKS_DIR / tid)
            except (ValueError, sqlite3.Error) as error:
                print(f"  FAILED '{tid}': {error}")
                skipped += 1
                continue
            imported += 1
        print(f"\nImported {imported} task(s), skipped {skipped}. "
              "Each task imports atomically on its own; a failure leaves earlier imports in place.")
        return

    print("Usage: relay recover --init | --from-projections [--decisions FILE] [--apply] | --regenerate-projections [task_id]")


# Python: Catching specific exception types (FileNotFoundError, ValueError,
#   OSError) lets each failure get its own clear message instead of a
#   stack trace.
def _recover_with_decisions(db_path: Path, decisions_path: Path, apply: bool) -> None:
    """--from-projections with a decisions file: rehearse every import in a
    throwaway in-memory database first, show where each task lands, and
    only with --apply write the clean and decision-resolved tasks."""
    try:
        decisions = db.load_decisions(decisions_path)
    except FileNotFoundError:
        print(f"Error: decisions file not found: {decisions_path}")
        sys.exit(1)
    except ValueError as error:
        print(f"Error: invalid decisions file: {error}")
        sys.exit(1)
    except OSError as error:
        print(f"Error: cannot read decisions file {decisions_path}: {error.strerror}")
        sys.exit(1)
    rehearsal = db.rehearse_migration(settings.TASKS_DIR, decisions)
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
        return
    if not db_path.exists():
        print(f"\nCannot --apply: no database at {db_path}. Run 'relay recover --init' first.")
        sys.exit(1)
    conn = db.connect_existing(db_path)
    imported, already_present, failed = 0, 0, 0
    for importable_task in rehearsal["clean"] + rehearsal["resolved"]:
        if db.task_exists(conn, importable_task):
            already_present += 1
            continue
        try:
            db.import_task(conn, importable_task, settings.TASKS_DIR / importable_task,
                           decision=decisions.get(importable_task))
        except (ValueError, sqlite3.Error) as error:
            print(f"  FAILED '{importable_task}': {error}")
            failed += 1
            continue
        imported += 1
    print(f"\nImported {imported} task(s); {already_present} already present; {failed} failed; "
          f"{len(rehearsal['excluded'])} excluded; {len(rehearsal['unresolved'])} unresolved. "
          "Each task imports atomically on its own.")


def cmd_register(repo_path: str, alias: str | None = None):
    """Register a repository with relay. Only the central registry is
    written; nothing goes into the repository itself."""
    path = Path(repo_path).resolve()
    if not path.exists():
        print(f"Error: Path does not exist: {path}")
        sys.exit(1)
    if not path.is_dir():
        print(f"Error: Path is not a directory: {path}")
        sys.exit(1)

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
        return

    print(f"Registered: {alias} -> {path}")


def cmd_gate_check(task_id: str, config: dict):
    """Say whether a task is waiting, and on whom (plain-text form)."""
    payload = gate_check_payload(task_id, config)
    gate = payload["gate"]
    print(f"Task: {payload['task_id']}")
    print(f"  Status: {payload['status']}")
    print(f"  Phase: {payload['current_phase']}")
    kind = f" ({gate['kind']})" if gate["kind"] else ""
    print(f"  Gate: {gate['state']}{kind}")
    print(f"  Reason: {gate['reason']}")
    if "fix_rounds" in gate:
        _print_fix_rounds(gate["fix_rounds"])
    if gate["next_phase"]:
        print(f"  Next phase: {gate['next_phase']}")
    if gate["send_back_command"]:
        recommended = " (recommended)" if gate["send_back_recommended"] else ""
        print(f"  To send back{recommended}: {gate['send_back_command']}")
    if gate["approve_command"]:
        print(f"  To approve: {gate['approve_command']}")


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
