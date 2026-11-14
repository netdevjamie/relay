"""The JSON read model: one description of a task, its latest step and its
gate, for every front end. Read-only by construction.
"""

import codecs
import json
from pathlib import Path

import db
import project_profile
import settings
import sizing
from settings import (
    VALID_VERDICTS, _CLI_CMD, _resolve_repo_path, _sqlite_enabled, get_db_connection,
)
from run_state import _is_agent_artifact, frontmatter_in, load_run_state
from routing import (
    GATE_APPROVAL, GATE_BLOCKED, GATE_DEPLOY_APPROVAL, GATE_RETRY_LIMIT, ROUTE_ADVANCE,
    ROUTE_COMPLETE, ROUTE_COMPLETE_SKIPPING_OPS, ROUTE_RETRY_LIMIT, SEND_BACK_GATE_KINDS,
    _deploy_flag, decide_routing, fix_round_summary,
)
from recorded_runs import is_test_command_confirmed, latest_test_run, read_test_runs
from owner_notes import read_owner_notes


# ---------------------------------------------------------------------------
# Read API (JSON)
# ---------------------------------------------------------------------------
# One read model for every front end (Claude Code pane, iTerm2 badges,
# `relay card`): `relay status|list|gate-check --json`. Front ends show what
# these return and compute nothing themselves.
#
# Nothing in this section writes: not to the ledger, not to task folders.
# In particular it never syncs a waiting artifact's verdict into the ledger
# (that is `relay next`'s job); it reports the verdict with recorded=false.
#
# The shapes are a contract (docs/json-api.md, tests/test_read_api.py).
# Adding a key is fine; renaming or removing one, or changing what a value
# means, is a new schema_version.
#
# 2: `retries` counts automatic rounds only (in version 1 it also counted
#    send-backs), and a task the limit stopped reads as a `retry_limit` gate
#    that offers send-back (in version 1, `blocked` with no send_back_command).
# 3: the process audit and the checks on a report are gone: no `audits` on
#    a status, no `audit` on a list row, no `process_audit` gate, no
#    `relay_found`, and `fix_rounds.lines` is one sentence.

JSON_SCHEMA_VERSION = 3

GATE_READY = "ready"                        # `relay next` will move on without the owner
GATE_WAITING_ON_AGENT = "waiting_on_agent"  # the current step has no verdict yet
GATE_WAITING_ON_OWNER = "waiting_on_owner"  # `relay next` would stop for the owner
GATE_CLOSED = "closed"                      # completed or abandoned

ERROR_TASK_NOT_FOUND = "task_not_found"
ERROR_LEDGER_UNAVAILABLE = "ledger_unavailable"
ERROR_INTERNAL = "internal_error"
ERROR_REPO_NOT_FOUND = "repo_not_found"


FRONTMATTER_READ_BYTES = 64 * 1024

# What reading a task's events raises when one payload is not JSON
# (ValueError), is nested too deep (RecursionError), or is JSON but not the
# object a reader expects (TypeError, AttributeError). Only a hand-edited
# ledger holds such a row.
_EVENTS_UNREADABLE = (ValueError, RecursionError, TypeError, AttributeError)


class TaskNotFound(LookupError):
    """No task with this id in the ledger (or, on the files backend, on disk)."""


def _frontmatter_or_empty(path: Path) -> dict:
    """An artifact's frontmatter, or {} when it is missing or unreadable.
    Readers must not crash on a half-written artifact, and must not open
    anything but a regular file: opening a named pipe waits for good.

    Only the first FRONTMATTER_READ_BYTES of the file are read. This runs
    for every task on every list, so a huge file in a task folder
    must not cost a huge read. Frontmatter that does not close
    within that much reads as none."""
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as artifact:
            head = artifact.read(FRONTMATTER_READ_BYTES)
        # final=False: a character cut in two by the cap is not an error.
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        # Line ends as Path.read_text() gives them, which this read replaced.
        # frontmatter_in gives {} for frontmatter that is not read.
        return frontmatter_in(text.replace("\r\n", "\n").replace("\r", "\n"))
    except (OSError, UnicodeDecodeError):
        return {}


def _artifact_for_step(task_id: str, step: int) -> str | None:
    """Files backend: the name of the artifact written for a step, if any."""
    task_dir = settings.TASKS_DIR / task_id
    if not task_dir.exists():
        return None
    names = sorted(f.name for f in task_dir.iterdir()
                   if _is_agent_artifact(f) and f.name.startswith(f"{step:02d}-"))
    return names[-1] if names else None


def _step_started(state: dict, step) -> str | None:
    """When a step was dispatched: the timestamp of its history entry."""
    for entry in state.get("history") or []:
        if isinstance(entry, dict) and entry.get("step") == step:
            return _timestamp_or_none(entry.get("timestamp"))
    return None


def _step_artifacts(task_id: str, state: dict, config: dict) -> dict:
    """The file name recorded for each of the task's steps, by step number.
    A name is recorded when the step is dispatched, so the file itself may
    not exist yet."""
    if _sqlite_enabled(config):
        return db.get_step_artifacts(get_db_connection(), task_id)
    steps = [entry.get("step") for entry in state.get("history") or [] if isinstance(entry, dict)]
    return {step: _artifact_for_step(task_id, step) for step in steps if isinstance(step, int)}


def read_latest_step(task_id: str, state: dict, config: dict) -> dict | None:
    """The task's newest step and its verdict, without writing anything.

    `recorded` is False when the verdict is so far only in the artifact file:
    the agent has finished, and `relay next` has not picked it up yet."""
    if _sqlite_enabled(config):
        # No task_dir argument: that is what makes get_latest_step sync
        # the verdict into the ledger, and a reader must not write.
        row = db.get_latest_step(get_db_connection(), task_id)
        if row is None:
            return None
        step, phase, recorded_verdict = row["step"], row["phase"], row["verdict"]
        artifact = row["artifact_filename"]
    else:
        entries = [entry for entry in state.get("history") or [] if isinstance(entry, dict)]
        if not entries:
            return None
        step, phase = entries[-1].get("step"), entries[-1].get("phase")
        recorded_verdict = entries[-1].get("verdict")
        artifact = _artifact_for_step(task_id, step) if isinstance(step, int) else None

    verdict = recorded_verdict
    if verdict is None and artifact:
        waiting = _frontmatter_or_empty(settings.TASKS_DIR / task_id / artifact).get("verdict")
        if waiting in VALID_VERDICTS.get(phase, []):
            verdict = waiting
    return {
        "step": step,
        "phase": phase,
        "verdict": verdict,
        "recorded": recorded_verdict is not None,
        "started": _step_started(state, step),
        "artifact": artifact,
        "artifact_path": str(settings.TASKS_DIR / task_id / artifact) if artifact else None,
    }


def _gate(state_name: str, reason: str, *, kind: str | None = None,
          next_phase: str | None = None, approve_command: str | None = None,
          send_back_command: str | None = None, send_back_recommended: bool = False,
          fix_rounds: dict | None = None) -> dict:
    """`fix_rounds` says how many fix rounds have run, where the tripwire
    stopped the task (routing.fix_round_summary). It is there only at such a
    gate: every other gate reads exactly as it did before it."""
    gate = {"state": state_name, "kind": kind, "next_phase": next_phase,
            "reason": reason, "approve_command": approve_command,
            "send_back_command": send_back_command,
            "send_back_recommended": send_back_recommended}
    if fix_rounds:
        gate["fix_rounds"] = fix_rounds
    return gate


def _records_for_step(task_id: str, latest_step: dict, config: dict) -> dict:
    """What Relay recorded about the latest step that routing reads: the
    plan's deploy flag and Relay's test run."""
    planned_deploy = None
    # As in cmd_next: the Architect's plan must say whether the task deploys.
    if latest_step["phase"] == "architect" and latest_step["artifact_path"]:
        planned_deploy = _deploy_flag(
            _frontmatter_or_empty(Path(latest_step["artifact_path"])).get("deploy"))
    test_run = latest_test_run(task_id, config, latest_step["step"])
    return {"planned_deploy": planned_deploy, "test_run": test_run,
            "test_command_confirmed": is_test_command_confirmed(task_id, config, test_run)}


def _decided(state: dict, latest_step: dict, records: dict, config: dict) -> dict:
    decision = decide_routing(state, latest_step["phase"], latest_step["verdict"], config,
                              planned_deploy=records["planned_deploy"],
                              test_run=records["test_run"],
                              test_command_confirmed=records["test_command_confirmed"],
                              sizing=sizing.read_sizing(state["task_id"], config))
    return {"decision": decision, "test_run": records["test_run"]}


def _paused_gate() -> dict:
    return _gate(GATE_WAITING_ON_OWNER, kind=GATE_BLOCKED,
                 reason="Task is paused; the owner decides how to continue.")


def read_gate(task_id: str, state: dict, latest_step: dict | None, config: dict) -> dict:
    """Where the task stands: can it move on, and if not, who is it waiting on?

    Uses decide_routing(), the same decision `relay next` acts on, and like
    `relay next` it never treats a blocked task as ready. A blocked task is
    one the limit on automatic rounds stopped (a `retry_limit` gate, as it
    was before `relay next` stopped it) or, failing that, a paused one."""
    status = state.get("status")
    if status in ("completed", "abandoned"):
        return _gate(GATE_CLOSED, f"Task is {status}.")
    # `relay next` refuses a blocked task too: the owner has to act.
    if latest_step is None or latest_step["verdict"] is None:
        if status == "blocked":
            return _paused_gate()
        phase = latest_step["phase"] if latest_step else state.get("current_phase")
        return _gate(GATE_WAITING_ON_AGENT, f"No verdict from {phase} yet.")

    try:
        records = _records_for_step(task_id, latest_step, config)
    except _EVENTS_UNREADABLE:
        # A blocked task read as blocked before its events were ever looked
        # at. It still does when one of them cannot be read, so that event
        # cannot fail the list for every other task.
        if status != "blocked":
            raise
        return _paused_gate()
    return _gate_for_decision(task_id, state, _decided(state, latest_step, records, config), config)


def _gate_for_decision(task_id: str, state: dict, decided: dict, config: dict) -> dict:
    decision = decided["decision"]
    if state.get("status") == "blocked" and decision.outcome != ROUTE_RETRY_LIMIT:
        return _paused_gate()
    if decision.outcome in (ROUTE_COMPLETE, ROUTE_COMPLETE_SKIPPING_OPS):
        return _gate(GATE_READY, decision.reason, next_phase="done")
    if decision.outcome == ROUTE_ADVANCE:
        return _gate(GATE_READY, decision.reason, next_phase=decision.next_phase)

    approve_command = None
    send_back_command = None
    if decision.gate_kind == GATE_APPROVAL:
        approve_command = f"{_CLI_CMD} override {task_id} {decision.next_phase}"
    elif decision.gate_kind == GATE_DEPLOY_APPROVAL:
        approve_command = f"{_CLI_CMD} override {task_id} done"
    if decision.gate_kind in SEND_BACK_GATE_KINDS:
        send_back_command = f"{_CLI_CMD} send-back {task_id}"
    fix_rounds = fix_round_summary(state) if decision.lists_fix_rounds else None
    return _gate(GATE_WAITING_ON_OWNER, decision.reason, kind=decision.gate_kind,
                 next_phase=decision.next_phase, approve_command=approve_command,
                 send_back_command=send_back_command,
                 send_back_recommended=decision.send_back_recommended,
                 fix_rounds=fix_rounds)


def read_approvals(task_id: str, config: dict) -> list[dict]:
    """Owner approvals recorded in the ledger, oldest first. The files
    backend keeps no approval record, so it always reports none."""
    if not _sqlite_enabled(config):
        return []
    approvals = []
    for event in db.get_events(get_db_connection(), task_id):
        if event["event_type"] != db.EVENT_APPROVAL:
            continue
        payload = event.get("payload") or {}
        approvals.append({"phase": event["phase"], "next_phase": payload.get("next_phase"),
                          "method": payload.get("method"), "note": payload.get("note"),
                          "timestamp": event["timestamp"]})
    return approvals


def read_interruptions(task_id: str, config: dict) -> list[dict]:
    """Times the machine slept while one of the task's steps was open,
    oldest first. The files backend keeps no events, so it has none."""
    if not _sqlite_enabled(config):
        return []
    return [{"phase": event["phase"], "step": event["step"], "noticed": event["timestamp"],
             **(event.get("payload") or {})}
            for event in db.get_events(get_db_connection(), task_id)
            if event["event_type"] == db.EVENT_SLEPT_DURING_STEP]


def _task_summary(task_id: str, state: dict) -> dict:
    return {
        "task_id": state.get("task_id", task_id),
        "repo": state.get("repo"),
        "status": state.get("status"),
        "current_phase": state.get("current_phase"),
        "deploy_intent": bool(state.get("deploy_intent")),
        "created": _timestamp_or_none(state.get("created")),
        "updated": _timestamp_or_none(state.get("updated")),
    }


def _timestamp_or_none(value) -> str | None:
    """Timestamps as text. Old run.yaml files hold some as YAML datetimes."""
    return None if value is None else str(value)


def _envelope(schema: str, body: dict) -> dict:
    return {"schema": schema, "schema_version": JSON_SCHEMA_VERSION, "ok": True, **body}


def error_payload(code: str, message: str) -> dict:
    return {"schema": "relay.error", "schema_version": JSON_SCHEMA_VERSION, "ok": False,
            "error": {"code": code, "message": message}}


def _require_state(task_id: str, config: dict) -> dict:
    state = load_run_state(task_id, config)
    if not state:
        raise TaskNotFound(f"No run state for {task_id}.")
    return state


def status_payload(task_id: str, config: dict) -> dict:
    """`relay status <task> --json`: everything known about one task."""
    state = _require_state(task_id, config)
    latest_step = read_latest_step(task_id, state, config)
    artifacts = _step_artifacts(task_id, state, config)
    history = [{"step": entry.get("step"), "phase": entry.get("phase"),
                "verdict": entry.get("verdict"),
                "timestamp": _timestamp_or_none(entry.get("timestamp")),
                "artifact": (artifacts.get(entry.get("step"))
                             if isinstance(entry.get("step"), int) else None)}
               for entry in state.get("history") or [] if isinstance(entry, dict)]
    task = _task_summary(task_id, state)
    task.update({"next_step": state.get("next_step"), "worktree_path": state.get("worktree_path"),
                 "branch": state.get("branch"), "base": state.get("base")})
    return _envelope("relay.status", {
        "task": task,
        "retries": dict(state.get("retries") or {}),
        "max_retries": config.get("max_retries", 3),
        "history": history,
        "latest_step": latest_step,
        "gate": read_gate(task_id, state, latest_step, config),
        "approvals": read_approvals(task_id, config),
        "test_runs": read_test_runs(task_id, config),
        "interruptions": read_interruptions(task_id, config),
        "owner_notes": read_owner_notes(task_id, config),
    })


def gate_check_payload(task_id: str, config: dict) -> dict:
    """`relay gate-check <task> --json`: is this task waiting, and on whom?"""
    state = _require_state(task_id, config)
    latest_step = read_latest_step(task_id, state, config)
    return _envelope("relay.gate-check", {
        "task_id": state.get("task_id", task_id),
        "status": state.get("status"),
        "current_phase": state.get("current_phase"),
        "latest_step": latest_step,
        "gate": read_gate(task_id, state, latest_step, config),
        "approvals": read_approvals(task_id, config),
    })


def _all_task_states(config: dict) -> list[tuple[str, dict]]:
    if _sqlite_enabled(config):
        return [(state["task_id"], state) for state in db.list_tasks(get_db_connection())]
    if not settings.TASKS_DIR.exists():
        return []
    task_ids = sorted(d.name for d in settings.TASKS_DIR.iterdir() if d.is_dir() and (d / "run.yaml").exists())
    states = [(task_id, load_run_state(task_id, config)) for task_id in task_ids]
    return [(task_id, state) for task_id, state in states if state]


def _latest_step_in_brief(latest_step: dict | None) -> dict | None:
    """A list row says which step is newest and since when, not where its file is."""
    if latest_step is None:
        return None
    return {key: latest_step[key] for key in ("step", "phase", "verdict", "started")}


def list_payload(config: dict) -> dict:
    """`relay list --json`: every task, with its gate in brief."""
    tasks = []
    for task_id, state in _all_task_states(config):
        latest_step = read_latest_step(task_id, state, config)
        gate = read_gate(task_id, state, latest_step, config)
        stopped = gate["kind"] == GATE_RETRY_LIMIT
        tasks.append({**_task_summary(task_id, state),
                      "gate_state": gate["state"], "gate_kind": gate["kind"],
                      "automatic_rounds": ((state.get("retries") or {}).get(gate["next_phase"], 0)
                                           if stopped else None),
                      "fix_rounds": gate["fix_rounds"]["count"] if "fix_rounds" in gate else None,
                      "latest_step": _latest_step_in_brief(latest_step)})
    return _envelope("relay.list", {"tasks": tasks})


def print_json(payload: dict) -> None:
    # default=str: an old run.yaml can hold values JSON has no type for
    # (YAML dates); print them as text rather than fail.
    print(json.dumps(payload, indent=2, default=str))


class RepoNotFound(LookupError):
    """Neither a registered repo alias nor a folder on disk."""


def _repo_path_for(alias_or_path: str) -> Path:
    """A registered alias wins; otherwise the argument is taken as a folder."""
    registered = _resolve_repo_path(alias_or_path)
    if registered:
        return registered
    folder = Path(alias_or_path).expanduser()
    if folder.is_dir():
        return folder
    raise RepoNotFound(f"'{alias_or_path}' is not a registered repo alias or a folder.")


def profile_payload(alias_or_path: str) -> dict:
    """`relay profile <repo> --json`: the repo's project profile and every
    problem found in it. A missing or invalid profile is still an answer
    (ok is true, valid is false); only an unknown repo is an error."""
    repo_path = _repo_path_for(alias_or_path)
    report = project_profile.load_profile(repo_path)
    return _envelope("relay.profile", {
        "repo": alias_or_path,
        "repo_path": str(repo_path),
        "profile_path": str(report.path),
        "found": report.found,
        "valid": report.valid,
        "errors": report.errors,
        "warnings": report.warnings,
        "profile": report.profile,
    })
