"""A task's run state: loading and saving it, reading agent artifacts,
counting automatic rounds, and checking state against what is on disk.

Under the sqlite backend the ledger (db.py) is the store; under the legacy
files backend it is run.yaml in the task folder.

Under sqlite the state dict is a read view of the ledger (load_run_state),
and each thing that happens is recorded when it happens, by its own call:
create_run, dispatch_step, record_verdict, count_retry. save_run_state
writes only the task's own fields; it never reads history or retries from
the dict. A command wraps the writes of one decision in ledger_writes()
so they commit together, and run.yaml is regenerated from the ledger once
they have.
Under the files backend the same calls change the dict and save_run_state
writes it to run.yaml.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import bounded_yaml
import db
import settings
from printable import printable
from settings import (
    FIRST_PHASE, PIPELINE_ORDER, _sqlite_enabled, dump_yaml, get_db_connection, load_yaml,
)


# ---------------------------------------------------------------------------
# Run state management
# ---------------------------------------------------------------------------

def run_state_path(task_id: str) -> Path:
    return settings.TASKS_DIR / task_id / "run.yaml"


def load_run_state(task_id: str, config: dict | None = None) -> dict | None:
    if config is not None and _sqlite_enabled(config):
        return db.get_task(get_db_connection(), task_id)
    p = run_state_path(task_id)
    if not p.exists():
        return None
    with open(p) as f:
        return load_yaml(f.read())


@contextmanager
def ledger_writes(task_id: str, config: dict | None):
    """Wrap one command's ledger writes: under sqlite they land in one
    transaction, all or nothing, and once the outermost block has
    committed, run.yaml is regenerated from the ledger. If the block raises,
    nothing is committed and run.yaml is not touched. Under the files
    backend it does nothing."""
    if config is None or not _sqlite_enabled(config):
        yield
        return
    conn = get_db_connection()
    outermost = not conn.in_transaction
    with db.transaction(conn):
        yield
    if outermost:
        _write_projection(conn, task_id)


def _write_projection(conn: sqlite3.Connection, task_id: str) -> None:
    """Projection-on-write: every committed ledger write regenerates
    run.yaml so it never drifts stale under the sqlite backend. This runs
    strictly after the commit -- a projection failure must never look like
    (or cause) a canonical-state failure, so it is caught and surfaced as a
    clearly labeled warning rather than raised. The canonical mutation
    that already succeeded is not rolled back for it.

    sqlite3.Error is deliberately NOT lumped in with ordinary projection
    failures (a bad path, a permissions error writing the file): an error
    surfacing from the SAME connection immediately after a successful
    commit on it means the canonical database connection itself may now be
    unhealthy (e.g. disk failure, the file removed out from under the
    process) -- a materially more serious situation than "the projection
    file didn't write", so it gets a distinct, louder warning pointing at
    the database itself rather than just the projection."""
    try:
        db.write_projection(conn, task_id, settings.TASKS_DIR / task_id)
    except sqlite3.Error as e:
        print(f"Warning: canonical state for '{task_id}' was saved successfully, "
              f"but the database connection raised an error while regenerating "
              f"its run.yaml projection immediately afterward: {e}")
        print("  This may indicate the canonical database itself is now "
              "unhealthy (not just the projection file). Check its integrity "
              "before continuing -- e.g. by restarting relay and letting "
              "get_db_connection()'s integrity check run again.")
    except Exception as e:
        print(f"Warning: canonical state for '{task_id}' was saved successfully, "
              f"but regenerating its run.yaml projection failed: {e}")
        print("  Canonical (SQLite) state is unaffected. Run "
              f"'relay recover --regenerate-projections {task_id}' once the "
              "underlying issue is fixed.")


def save_run_state(task_id: str, state: dict, config: dict | None = None):
    """Save the task's own fields: status, phase, next step, deploy intent,
    repo, checkout and the time. Under sqlite this never reads `history` or
    `retries` from the dict: steps, verdicts and automatic rounds are
    recorded by their own calls, when they happen."""
    if config is not None and _sqlite_enabled(config):
        with ledger_writes(task_id, config):
            db.update_task(get_db_connection(), task_id, status=state.get("status"),
                           current_phase=state.get("current_phase"),
                           next_step=state.get("next_step"), updated=state.get("updated"),
                           deploy_intent=state.get("deploy_intent"), repo=state.get("repo"),
                           worktree_path=state.get("worktree_path"), branch=state.get("branch"),
                           base=state.get("base"))
        return
    p = run_state_path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file, then rename (POSIX atomic)
    tmp = p.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        f.write(dump_yaml(state))
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(p)


def create_run(task_id: str, state: dict, config: dict | None = None) -> None:
    """Record a new task from the run state `relay start` built."""
    if config is not None and _sqlite_enabled(config):
        with ledger_writes(task_id, config):
            db.create_task(get_db_connection(), task_id, repo=state.get("repo"),
                           deploy_intent=bool(state.get("deploy_intent")),
                           current_phase=state.get("current_phase"), created=state.get("created"),
                           updated=state.get("updated"), worktree_path=state.get("worktree_path"),
                           branch=state.get("branch"), base=state.get("base"),
                           next_step=state.get("next_step", 1))
        return
    save_run_state(task_id, state, config)


def dispatch_step(task_id: str, state: dict, phase: str, step: int, artifact_filename: str,
                  config: dict | None = None) -> None:
    """A step starts: `phase` becomes the task's phase, the step goes into
    the history, and under sqlite the ledger records it with the report
    file its agent was told to write. The caller saves the task after."""
    state["current_phase"] = phase
    state["next_step"] = step + 1
    record_phase(state, phase, step)
    if config is not None and _sqlite_enabled(config):
        db.record_dispatch(get_db_connection(), task_id, phase=phase, step=step,
                           artifact_filename=artifact_filename,
                           created_at=state["history"][-1]["timestamp"],
                           task_dir=settings.TASKS_DIR / task_id)


def record_verdict(task_id: str, state: dict, phase: str, step: int, verdict: str,
                   config: dict | None = None) -> None:
    """Step `step` of `phase` has finished and its verdict is read: it goes
    on that step's history entry and, under sqlite, on that step's row.
    A step with no verdict to read (an Arbiter report Relay could not read,
    sent back) records nothing in the ledger."""
    _record_verdict_in_history(state, phase, step, verdict)
    if verdict and config is not None and _sqlite_enabled(config):
        db.record_verdict(get_db_connection(), task_id, step=step, phase=phase, verdict=verdict,
                          task_dir=settings.TASKS_DIR / task_id)


def count_retry(task_id: str, state: dict, phase: str, config: dict | None = None) -> None:
    """Relay routes the task back to `phase` on its own: one automatic
    round, counted in the dict and, under sqlite, as a retry event."""
    increment_retry(state, phase)
    if config is not None and _sqlite_enabled(config):
        db.record_retry(get_db_connection(), task_id, phase=phase)


def init_run_state(task_id: str) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "task_id": task_id,
        "created": now,
        "updated": now,
        "status": "in_progress",
        "current_phase": FIRST_PHASE,
        "next_step": 1,
        "schema_version": 1,
        "repo": None,
        "worktree_path": None,
        "branch": None,
        "base": None,
        "deploy_intent": False,
        "retries": {"builder": 0, "reviewer": 0},
        "history": [],
    }


def record_phase(state: dict, phase: str, step: int, verdict: str | None = None):
    """Add a step to the history in memory. dispatch_step() is what a
    command calls; this alone records nothing in the ledger."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "phase": phase,
        "step": step,
        "timestamp": now,
    }
    if verdict:
        entry["verdict"] = verdict
    if not isinstance(state.get("history"), list):
        state["history"] = []
    state["history"].append(entry)
    state["updated"] = now


# ---------------------------------------------------------------------------
# Artifact parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a markdown file: {} when there is no
    regular file there or its bytes are not UTF-8 (db.report_text)."""
    text = db.report_text(path)
    return {} if text is None else frontmatter_in(text)


def frontmatter_in(text: str) -> dict:
    """The YAML frontmatter at the top of a markdown text, {} when it has
    none, and when it has one that is not read (bounded_yaml.py) or is not
    YAML: a report whose frontmatter cannot be read has no verdict."""
    try:
        return bounded_yaml.frontmatter(text)
    except bounded_yaml.NotRead:
        return {}


def _is_agent_artifact(f: Path) -> bool:
    """True if the file is a numbered agent artifact (not human artifacts 90-99)."""
    if f.suffix != ".md" or not f.name[0:2].isdigit():
        return False
    step = int(f.name[0:2])
    return step < 90  # 90-99 reserved for human artifacts (98=session, 99=final)


def find_latest_artifact(task_id: str, config: dict | None = None) -> tuple[Path | None, dict]:
    """Find the most recent numbered agent artifact (by filename) in a task folder.

    Under the sqlite backend this is the direct replacement for
    parse_frontmatter()-driven routing: the frontmatter-shaped dict comes
    from the steps table, not from re-reading the file.
    """
    if config is not None and _sqlite_enabled(config):
        row = db.get_latest_step(get_db_connection(), task_id, task_dir=settings.TASKS_DIR / task_id)
        if row is None:
            return None, {}
        path = settings.TASKS_DIR / task_id / row["artifact_filename"]
        frontmatter = {
            "task_id": row["task_id"],
            "phase": row["phase"],
            "step": row["step"],
            "verdict": row["verdict"],
        }
        return path, frontmatter
    task_dir = settings.TASKS_DIR / task_id
    if not task_dir.exists():
        return None, {}
    artifacts = sorted(
        [f for f in task_dir.iterdir() if _is_agent_artifact(f)],
        key=lambda f: f.name,
    )
    if not artifacts:
        return None, {}
    latest = artifacts[-1]
    return latest, parse_frontmatter(latest)


def get_next_step(task_id: str, config: dict | None = None) -> int:
    """Determine the next step number for artifact naming."""
    if config is not None and _sqlite_enabled(config):
        return db.get_next_step_number(get_db_connection(), task_id)
    task_dir = settings.TASKS_DIR / task_id
    if not task_dir.exists():
        return 1
    existing = [f for f in task_dir.iterdir() if _is_agent_artifact(f)]
    if not existing:
        return 1
    max_step = max(int(f.name[0:2]) for f in existing)
    return max_step + 1


# ---------------------------------------------------------------------------
# State consistency checks
# ---------------------------------------------------------------------------
# These detect contradictions between run.yaml and artifacts on disk.
# Warnings are printed but do not block routing. The orchestrator routes
# from artifact truth (latest verdict), not run.yaml.current_phase.

EXPECTED_FLOW_ORDER = PIPELINE_ORDER


def check_state_consistency(task_id: str, state: dict, artifact_phase: str, artifact_step: int):
    """Compare run.yaml state against artifact reality. Print warnings for contradictions."""
    warnings = []
    current_phase = state.get("current_phase", "")

    # 1. Phase mismatch: run.yaml says one phase, latest artifact is from another
    if current_phase and current_phase != artifact_phase and current_phase != "done":
        warnings.append(
            f"run.yaml current_phase is '{current_phase}' but latest artifact is from "
            f"'{printable(artifact_phase)}'. Routing from artifact truth ('{printable(artifact_phase)}')."
        )

    # 2. Missing artifact: history says a phase ran at a step, but no file exists
    task_dir = settings.TASKS_DIR / task_id
    history = state.get("history", [])
    for entry in history:
        if not isinstance(entry, dict):
            continue
        hist_step = entry.get("step")
        hist_phase = entry.get("phase")
        if hist_step is None or hist_phase is None:
            continue
        # Check if an artifact file exists for this history entry
        expected_prefix = f"{hist_step:02d}-"
        matches = [f for f in task_dir.iterdir() if _is_agent_artifact(f) and f.name.startswith(expected_prefix)]
        if not matches:
            warnings.append(
                f"History records '{hist_phase}' at step {hist_step:02d} but no "
                f"{expected_prefix}*.md artifact exists on disk."
            )

    # 3. Skipped phase: check if the normal flow has gaps in the artifact trail
    artifact_phases_on_disk = []
    for f in sorted(task_dir.iterdir(), key=lambda f: f.name):
        if _is_agent_artifact(f):
            fm = parse_frontmatter(f)
            p = fm.get("phase")
            if p and p in EXPECTED_FLOW_ORDER:
                artifact_phases_on_disk.append(p)
    # Check for gaps: if we have architect and tester but no builder/reviewer
    if len(artifact_phases_on_disk) >= 2:
        first_idx = EXPECTED_FLOW_ORDER.index(artifact_phases_on_disk[0]) if artifact_phases_on_disk[0] in EXPECTED_FLOW_ORDER else 0
        last_idx = EXPECTED_FLOW_ORDER.index(artifact_phases_on_disk[-1]) if artifact_phases_on_disk[-1] in EXPECTED_FLOW_ORDER else 0
        expected_span = EXPECTED_FLOW_ORDER[first_idx:last_idx + 1]
        # Remove duplicates from artifact list (fix passes create multiple builder entries)
        seen_phases = []
        for p in artifact_phases_on_disk:
            if p not in seen_phases:
                seen_phases.append(p)
        missing = [p for p in expected_span if p not in seen_phases]
        if missing:
            warnings.append(
                f"Artifacts span {artifact_phases_on_disk[0]}..{artifact_phases_on_disk[-1]} "
                f"but no artifact exists for: {', '.join(missing)}. "
                f"Phase(s) may have been skipped."
            )

    if warnings:
        print(f"\n  STATE WARNINGS:")
        for w in warnings:
            print(f"    - {w}")
        print(f"  (Routing proceeds from latest artifact verdict. Use 'override' if this is wrong.)")


# ---------------------------------------------------------------------------
# Retry tracking
# ---------------------------------------------------------------------------
# state["retries"][phase] is the number of automatic rounds into that phase
# in a row: times Relay routed the task back to it on its own after the owner
# last sent work back to it or overrode into it. The limit (max_retries)
# stops only those rounds; a round the owner chose is never counted.

def check_retry_limit(state: dict, phase: str, config: dict) -> bool:
    """Returns True if under the retry limit."""
    max_r = config.get("max_retries", 3)
    retries = state.get("retries", {})
    count = retries.get(phase, 0)
    return count < max_r


def increment_retry(state: dict, phase: str):
    """One more automatic round in memory. count_retry() is what a command
    calls; this alone records nothing in the ledger."""
    if "retries" not in state:
        state["retries"] = {}
    state["retries"][phase] = state["retries"].get(phase, 0) + 1


def reset_retries(state: dict, phase: str) -> None:
    """The owner chose to move the task into `phase`: its count of automatic
    rounds starts again. Under the sqlite backend the ledger works this out
    from the send-back or override event; this keeps the state in memory
    saying the same, for the rest of the command and for run.yaml under the
    files backend."""
    if state.get("retries", {}).get(phase):
        state["retries"][phase] = 0


# ---------------------------------------------------------------------------
# Fix rounds
# ---------------------------------------------------------------------------
# Unlike the retry count, the count of fix rounds counts every route into the
# Builder: automatic, a send-back, an override or a round an owner's note
# asked for. It serves only a fix_rounds gate an older Relay left standing
# (routing._arbiter_trigger); the count that calls the Arbiter is
# arbiter.loop_counts.

def is_builder_step(entry) -> bool:
    return isinstance(entry, dict) and entry.get("phase") == "builder"


def fix_rounds(state: dict) -> int:
    """How many fix rounds the task has had: every Builder step after its
    first. The first build is round 0."""
    builds = sum(is_builder_step(entry) for entry in state.get("history") or [])
    return max(builds - 1, 0)


def _record_verdict_in_history(state: dict, phase: str, step: int, verdict: str) -> None:
    """Attach the verdict to the history entry of step `step` of `phase`,
    in memory. No entry is marked when the history has no such step.
    record_verdict() is what a command calls."""
    for entry in state.get("history", []):
        if isinstance(entry, dict) and entry.get("phase") == phase and entry.get("step") == step:
            entry["verdict"] = verdict
