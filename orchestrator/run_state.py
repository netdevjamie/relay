"""A task's run state: loading and saving it, reading agent artifacts,
counting automatic rounds, and checking state against what is on disk.

Under the sqlite backend the ledger (db.py) is the store; under the legacy
files backend it is run.yaml in the task folder.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import bounded_yaml
import db
import settings
import sizing
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


def save_run_state(task_id: str, state: dict, config: dict | None = None):
    if config is not None and _sqlite_enabled(config):
        conn = get_db_connection()
        db.record_step_and_save(conn, task_id, state, task_dir=settings.TASKS_DIR / task_id)
        # Projection-on-write: every successful canonical mutation
        # regenerates run.yaml so it never drifts stale under the sqlite
        # backend. This runs strictly after the sqlite write above commits
        # -- a projection failure must never look like (or cause) a
        # canonical-state failure, so it is caught and surfaced as a
        # clearly labeled warning rather than raised. The canonical
        # mutation that already succeeded is not rolled back for it.
        #
        # sqlite3.Error is deliberately NOT lumped in with ordinary
        # projection failures (a bad path, a permissions error writing the
        # file): an error surfacing from the SAME connection immediately
        # after a successful commit on it means the canonical database
        # connection itself may now be unhealthy (e.g. disk failure,
        # the file removed out from under the process) -- a materially
        # more serious situation than "the projection file didn't write",
        # so it gets a distinct, louder warning pointing at the database
        # itself rather than just the projection.
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
    """Extract YAML frontmatter from a markdown file."""
    if not path.exists():
        return {}
    return frontmatter_in(path.read_text())


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
    from the steps table, not from re-reading the file: routing state comes
    from the ledger, never from a report's frontmatter.
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
# These detect contradictions between the recorded run state (the ledger, or
# run.yaml on the files backend) and artifacts on disk. Warnings are printed
# but do not block routing. The orchestrator routes from artifact truth
# (latest verdict), not the recorded current_phase.

EXPECTED_FLOW_ORDER = PIPELINE_ORDER


def check_state_consistency(task_id: str, state: dict, artifact_phase: str, artifact_step: int,
                            config: dict | None = None):
    """Compare the recorded run state against artifact reality. Print warnings for contradictions.
    A phase the task's sizing lets it pass by (see sizing.phases_it_may_skip)
    is never reported as skipped."""
    warnings = []
    current_phase = state.get("current_phase", "")

    # 1. Phase mismatch: the state says one phase, latest artifact is from another
    if current_phase and current_phase != artifact_phase and current_phase != "done":
        warnings.append(
            f"The recorded phase is '{current_phase}' but the latest report is from "
            f"'{artifact_phase}'. Routing from artifact truth ('{artifact_phase}')."
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
        may_skip = sizing.phases_it_may_skip(sizing.read_sizing(task_id, config))
        missing = [p for p in expected_span if p not in seen_phases and p not in may_skip]
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
# in a row: times Relay routed the task back to it on its own since the owner
# last sent work back to it or overrode into it. The limit (max_retries)
# stops only those rounds; a round the owner chose is never counted.

def check_retry_limit(state: dict, phase: str, config: dict) -> bool:
    """Returns True if under the retry limit."""
    max_r = config.get("max_retries", 3)
    retries = state.get("retries", {})
    count = retries.get(phase, 0)
    return count < max_r


def increment_retry(state: dict, phase: str):
    if "retries" not in state:
        state["retries"] = {}
    state["retries"][phase] = state["retries"].get(phase, 0) + 1


def reset_retries(state: dict, phase: str) -> None:
    """The owner chose to move the task into `phase`: its count of automatic
    rounds starts again. Under the sqlite backend the ledger works this out
    from the send-back or override event; this keeps the state in memory
    saying the same, so the save that follows writes no retry event."""
    if state.get("retries", {}).get(phase):
        state["retries"][phase] = 0


# ---------------------------------------------------------------------------
# Fix rounds
# ---------------------------------------------------------------------------
# Unlike the retry count, the count of fix rounds counts every route into the
# Builder: automatic, a send-back, an override or a round an owner's note
# asked for. It is what the tripwire in routing.py reads.

def is_builder_step(entry) -> bool:
    return isinstance(entry, dict) and entry.get("phase") == "builder"


def fix_rounds(state: dict) -> int:
    """How many fix rounds the task has had: every Builder step after its
    first. The first build is round 0."""
    builds = sum(is_builder_step(entry) for entry in state.get("history") or [])
    return max(builds - 1, 0)


def _record_verdict_in_history(state: dict, phase: str, verdict: str) -> None:
    """Attach a finished phase's verdict to its history entry."""
    for entry in reversed(state.get("history", [])):
        if isinstance(entry, dict) and entry.get("phase") == phase and not entry.get("verdict"):
            entry["verdict"] = verdict
            break
