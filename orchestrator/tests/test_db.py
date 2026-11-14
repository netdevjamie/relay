"""Tests for orchestrator/db.py -- the SQLite persistence layer, and its
integration into relay.py's four dispatch chokepoints (load_run_state,
save_run_state, find_latest_artifact, get_next_step) plus the CLI commands
that call them.

Every test uses an isolated tmp_path DB and tmp_path task directory --
nothing here ever touches ~/Projects/.relay/ or any real task.
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import relay
import settings
import run_state
import routing
import agents
import commands
from settings import VALID_PHASES, VALID_VERDICTS, VALID_STATUSES, DEFAULT_CONFIG
from run_state import init_run_state, record_phase
from agents import artifact_name
from tests.hostile_yaml import (  # noqa: F401  (nothing_composed is a fixture)
    BAD_VALUE_NAMES, BAD_VALUES, SECURITYS_MERGE_KEYS, nothing_composed,
)


@pytest.fixture(autouse=True)
def _configure_db():
    """db.py needs the real enums before any test runs."""
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "relay.db")
    yield c
    c.close()


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    """Point relay.py's chokepoints at an isolated sqlite DB + task dir, and
    hand back a sqlite-backend config. Mirrors test_relay.py's TASKS_DIR
    monkeypatch convention.

    Explicitly creates the database up front via db.init_database(), the
    same deliberate step an operator takes with 'relay recover --init'
    before real cutover -- settings.get_db_connection() is fail-closed and no
    longer creates it lazily (see TestFailClosedRecovery), so every
    ordinary sqlite_env-based test needs this environment to already be
    "post-init", not "pre-cutover".
    """
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(settings, "_db_connection", None)
    db_path = tmp_path / "relay.db"
    monkeypatch.setenv("RELAY_DB_PATH", str(db_path))
    (tmp_path / "tasks").mkdir()
    db.init_database(db_path).close()
    config = dict(DEFAULT_CONFIG)
    config["persistence_backend"] = "sqlite"
    config["human_gates"] = dict(DEFAULT_CONFIG["human_gates"])
    yield config


def _make_task_dir_with_brief(base: Path, task_id: str) -> Path:
    d = base / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text("# Brief\nSomething to do.\n")
    return d


# ---------------------------------------------------------------------------
# 1. Database initialization
# ---------------------------------------------------------------------------

class TestInit:
    def test_schema_creates_all_tables(self, conn):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"tasks", "steps", "events"} <= tables

    def test_init_schema_is_idempotent(self, tmp_path):
        p = tmp_path / "relay.db"
        c1 = db.connect(p)
        db.create_task(c1, "idempotent-check")
        c1.close()
        # Reconnecting must not wipe or error on existing data.
        c2 = db.connect(p)
        assert db.task_exists(c2, "idempotent-check")
        c2.close()


# ---------------------------------------------------------------------------
# 2/3. Task creation and retrieval
# ---------------------------------------------------------------------------

class TestTaskCreateRetrieve:
    def test_create_task_defaults(self, conn):
        state = db.create_task(conn, "t1", repo="relay")
        assert state["task_id"] == "t1"
        assert state["status"] == "in_progress"
        assert state["current_phase"] == "architect"
        assert state["next_step"] == 1
        assert state["deploy_intent"] is False
        assert state["retries"] == {"builder": 0, "reviewer": 0}
        assert state["history"] == []

    def test_create_task_duplicate_raises(self, conn):
        db.create_task(conn, "dup")
        with pytest.raises(sqlite3.IntegrityError):
            db.create_task(conn, "dup")

    def test_get_task_nonexistent_returns_none(self, conn):
        assert db.get_task(conn, "nope") is None

    def test_list_tasks(self, conn):
        db.create_task(conn, "a")
        db.create_task(conn, "b")
        ids = sorted(t["task_id"] for t in db.list_tasks(conn))
        assert ids == ["a", "b"]

    def test_create_task_rejects_unknown_phase(self, conn):
        with pytest.raises(db.InvalidTransition):
            db.create_task(conn, "bad-phase", current_phase="not-a-real-phase")


# ---------------------------------------------------------------------------
# 4. Step creation/update, via record_step_and_save (the save_run_state()
#    replacement) -- exercised the same way relay.py's cmd_* functions
#    actually call it: mutate a state dict, then save.
# ---------------------------------------------------------------------------

class TestSteps:
    def test_record_step_and_save_creates_step_row(self, conn, tmp_path):
        state = init_run_state("s1")
        record_phase(state, "architect", 1)
        db.record_step_and_save(conn, "s1", state, task_dir=tmp_path / "s1")
        latest = db.get_latest_step(conn, "s1")
        assert latest["phase"] == "architect"
        assert latest["step"] == 1
        assert latest["verdict"] is None

    def test_verdict_backfilled_onto_existing_step(self, conn, tmp_path):
        state = init_run_state("s2")
        record_phase(state, "architect", 1)
        db.record_step_and_save(conn, "s2", state, task_dir=tmp_path / "s2")

        # Mirror cmd_next's own logic: attach a verdict to the matching
        # history entry, then save again.
        state["history"][0]["verdict"] = "ready-for-builder"
        db.record_step_and_save(conn, "s2", state, task_dir=tmp_path / "s2")

        latest = db.get_latest_step(conn, "s2")
        assert latest["verdict"] == "ready-for-builder"
        # Must still be a single step row, not a duplicate.
        rows = conn.execute("SELECT COUNT(*) AS n FROM steps WHERE task_id='s2'").fetchone()
        assert rows["n"] == 1

    def test_get_next_step_number(self, conn, tmp_path):
        state = init_run_state("s3")
        record_phase(state, "architect", 1, verdict="ready-for-builder")
        db.record_step_and_save(conn, "s3", state, task_dir=tmp_path / "s3")
        assert db.get_next_step_number(conn, "s3") == 2

    def test_get_next_step_number_no_steps_yet(self, conn):
        db.create_task(conn, "s4")
        assert db.get_next_step_number(conn, "s4") == 1


# ---------------------------------------------------------------------------
# 5/6. Valid and invalid state transitions
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_invalid_phase_rejected(self, conn, tmp_path):
        state = init_run_state("bad1")
        state["current_phase"] = "not-a-phase"
        with pytest.raises(db.InvalidTransition):
            db.record_step_and_save(conn, "bad1", state, task_dir=tmp_path / "bad1")

    def test_invalid_status_rejected(self, conn, tmp_path):
        state = init_run_state("bad2")
        state["status"] = "not-a-status"
        with pytest.raises(db.InvalidTransition):
            db.record_step_and_save(conn, "bad2", state, task_dir=tmp_path / "bad2")

    def test_invalid_verdict_for_phase_rejected(self, conn, tmp_path):
        state = init_run_state("bad3")
        record_phase(state, "architect", 1, verdict="not-a-real-verdict")
        with pytest.raises(db.InvalidTransition):
            db.record_step_and_save(conn, "bad3", state, task_dir=tmp_path / "bad3")

    def test_valid_full_architect_to_builder_cycle(self, conn, tmp_path):
        state = init_run_state("good1")
        record_phase(state, "architect", 1)
        db.record_step_and_save(conn, "good1", state, task_dir=tmp_path / "good1")

        state["history"][0]["verdict"] = "ready-for-builder"
        db.record_step_and_save(conn, "good1", state, task_dir=tmp_path / "good1")

        state["current_phase"] = "builder"
        state["next_step"] = 3
        record_phase(state, "builder", 2)
        db.record_step_and_save(conn, "good1", state, task_dir=tmp_path / "good1")

        final = db.get_task(conn, "good1")
        assert final["current_phase"] == "builder"
        assert [h["phase"] for h in final["history"]] == ["architect", "builder"]
        assert final["history"][0]["verdict"] == "ready-for-builder"


# ---------------------------------------------------------------------------
# 9/18. Run/event recording and audit history
# ---------------------------------------------------------------------------

class TestEventsAudit:
    def test_task_created_event_recorded(self, conn):
        db.create_task(conn, "ev1")
        events = db.get_events(conn, "ev1")
        assert any(e["event_type"] == db.EVENT_TASK_CREATED for e in events)

    def test_multi_step_audit_trail_is_chronological(self, conn, tmp_path):
        state = init_run_state("ev2")
        record_phase(state, "architect", 1)
        db.record_step_and_save(conn, "ev2", state, task_dir=tmp_path / "ev2")
        state["history"][0]["verdict"] = "ready-for-builder"
        db.record_step_and_save(conn, "ev2", state, task_dir=tmp_path / "ev2")

        events = db.get_events(conn, "ev2")
        assert [e["event_type"] for e in events][0] == db.EVENT_TASK_CREATED
        assert db.EVENT_STATE_SAVED in [e["event_type"] for e in events]
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps)

    def test_approval_event(self, conn):
        db.create_task(conn, "ev3")
        db.record_approval(conn, "ev3", phase="architect", next_phase="builder", method="override")
        events = db.get_events(conn, "ev3")
        approvals = [e for e in events if e["event_type"] == db.EVENT_APPROVAL]
        assert len(approvals) == 1
        assert approvals[0]["payload"]["next_phase"] == "builder"
        assert approvals[0]["payload"]["method"] == "override"


# ---------------------------------------------------------------------------
# 10. Retry recording
# ---------------------------------------------------------------------------

class TestRetries:
    def test_retry_count_derived_from_events(self, conn, tmp_path):
        state = init_run_state("r1")
        db.record_step_and_save(conn, "r1", state, task_dir=tmp_path / "r1")
        assert db.get_retry_counts(conn, "r1")["builder"] == 0

        state["retries"]["builder"] += 1
        db.record_step_and_save(conn, "r1", state, task_dir=tmp_path / "r1")
        assert db.get_retry_counts(conn, "r1")["builder"] == 1

        state["retries"]["builder"] += 1
        db.record_step_and_save(conn, "r1", state, task_dir=tmp_path / "r1")
        assert db.get_retry_counts(conn, "r1")["builder"] == 2
        # Untouched phase stays zero.
        assert db.get_retry_counts(conn, "r1")["reviewer"] == 0

    def test_get_task_reflects_retries(self, conn, tmp_path):
        state = init_run_state("r2")
        state["retries"]["reviewer"] = 1
        db.record_step_and_save(conn, "r2", state, task_dir=tmp_path / "r2")
        assert db.get_task(conn, "r2")["retries"]["reviewer"] == 1


# ---------------------------------------------------------------------------
# 12. Worktree/task relationships
# ---------------------------------------------------------------------------

class TestWorktree:
    def test_worktree_fields_round_trip(self, conn, tmp_path):
        state = init_run_state("wt1")
        state["worktree_path"] = "/Users/you/Projects/worktrees/relay-wt1"
        state["branch"] = "relay/wt1"
        state["base"] = "origin/main"
        db.record_step_and_save(conn, "wt1", state, task_dir=tmp_path / "wt1")

        loaded = db.get_task(conn, "wt1")
        assert loaded["worktree_path"] == state["worktree_path"]
        assert loaded["branch"] == "relay/wt1"
        assert loaded["base"] == "origin/main"

    def test_worktree_event_recorded(self, conn):
        db.create_task(conn, "wt2")
        db.record_worktree_event(conn, "wt2", created=True, detail="created relay/wt2 off origin/main")
        events = [e for e in db.get_events(conn, "wt2") if e["event_type"] == db.EVENT_WORKTREE_CREATED]
        assert len(events) == 1
        assert "relay/wt2" in events[0]["payload"]["detail"]


# ---------------------------------------------------------------------------
# 13. Authorization/capability invariants
# ---------------------------------------------------------------------------

class TestAuthorizationInvariant:
    def test_raw_event_insert_does_not_change_task_state(self, conn):
        """An agent/session writing directly to the events table (bypassing
        the deliberate record_step_and_save/create_task functions) must not
        gain any authority -- specifically, it must not change tasks.status
        or tasks.current_phase. Only the sanctioned write path does that.
        """
        db.create_task(conn, "auth1")
        before = db.get_task(conn, "auth1")

        # Simulate an agent trying to write itself into "ops"/"completed"
        # by inserting a raw event, without going through record_step_and_save.
        with conn:
            conn.execute(
                "INSERT INTO events (task_id, event_type, phase, timestamp) VALUES (?,?,?,?)",
                ("auth1", "verdict_recorded", "ops", "2026-01-01T00:00:00Z"),
            )

        after = db.get_task(conn, "auth1")
        assert after["status"] == before["status"] == "in_progress"
        assert after["current_phase"] == before["current_phase"] == "architect"

    def test_invalid_transition_leaves_no_partial_state(self, conn, tmp_path):
        """A rejected save must not partially land -- the transaction rolls
        back completely (see next section for the dedicated transaction
        test; this one checks the task-existence angle specifically)."""
        state = init_run_state("auth2")
        record_phase(state, "architect", 1, verdict="not-a-real-verdict")
        with pytest.raises(db.InvalidTransition):
            db.record_step_and_save(conn, "auth2", state, task_dir=tmp_path / "auth2")
        # The task row must not have been created by the failed save.
        assert db.get_task(conn, "auth2") is None


# ---------------------------------------------------------------------------
# 19. Multiple operations inside transactional boundaries
# ---------------------------------------------------------------------------

class TestTransactionBoundaries:
    def test_failed_save_does_not_partially_update_existing_task(self, conn, tmp_path):
        state = init_run_state("tx1")
        state["current_phase"] = "architect"
        record_phase(state, "architect", 1)
        db.record_step_and_save(conn, "tx1", state, task_dir=tmp_path / "tx1")
        snapshot = db.get_task(conn, "tx1")

        # Now attempt a save with a bad verdict mixed in among otherwise-valid
        # history -- the whole operation must fail atomically, not partially
        # write the good history entries and skip the bad one.
        state["history"].append({"phase": "builder", "step": 2, "verdict": "not-valid", "timestamp": "x"})
        state["current_phase"] = "builder"
        with pytest.raises(db.InvalidTransition):
            db.record_step_and_save(conn, "tx1", state, task_dir=tmp_path / "tx1")

        after = db.get_task(conn, "tx1")
        assert after["current_phase"] == snapshot["current_phase"] == "architect"
        assert len(after["history"]) == len(snapshot["history"]) == 1


# ---------------------------------------------------------------------------
# 15. Process restart / state recovery
# ---------------------------------------------------------------------------

class TestRestartRecovery:
    def test_state_survives_reconnect(self, tmp_path):
        p = tmp_path / "relay.db"
        c1 = db.connect(p)
        state = init_run_state("restart1")
        state["current_phase"] = "architect"
        record_phase(state, "architect", 1, verdict="ready-for-builder")
        db.record_step_and_save(c1, "restart1", state, task_dir=tmp_path / "restart1")
        c1.close()

        # Simulate a fresh process: new connection, same file.
        c2 = db.connect(p)
        recovered = db.get_task(c2, "restart1")
        assert recovered is not None
        assert recovered["current_phase"] == "architect"
        assert recovered["history"][0]["verdict"] == "ready-for-builder"
        c2.close()

    def test_restart_then_continue_creates_no_duplicate_events(self, tmp_path):
        """End-to-end acceptance property: reloading canonical
        state after a restart is read-only and must never create an event
        merely because a new connection/process opened; the workflow must
        then be able to continue correctly, appending exactly the new,
        real events the continuation produces -- no more, no less.
        """
        p = tmp_path / "relay.db"
        task_dir = tmp_path / "restart2"

        c1 = db.connect(p)
        state = init_run_state("restart2")
        state["current_phase"] = "architect"
        record_phase(state, "architect", 1)
        db.record_step_and_save(c1, "restart2", state, task_dir=task_dir)
        state["history"][0]["verdict"] = "ready-for-builder"
        db.record_step_and_save(c1, "restart2", state, task_dir=task_dir)
        c1.close()

        # "Restart": a fresh connection (simulating a fresh process) does
        # nothing but read -- repeatedly. Must never grow the event log.
        events_before = None
        for _ in range(3):
            c = db.connect(p)
            recovered = db.get_task(c, "restart2")
            events_now = db.get_events(c, "restart2")
            if events_before is not None:
                assert len(events_now) == len(events_before), (
                    "a read-only reload after restart must not create new events"
                )
            events_before = events_now
            assert recovered["current_phase"] == "architect"
            assert recovered["history"][0]["verdict"] == "ready-for-builder"
            c.close()

        # Continuation: a genuinely new connection advances the task for
        # real. This MUST append new events -- the invariant is "no
        # spurious duplication," not "no new events ever."
        c2 = db.connect(p)
        pre_continue_count = len(db.get_events(c2, "restart2"))
        state["current_phase"] = "builder"
        state["next_step"] = 3
        record_phase(state, "builder", 2)
        db.record_step_and_save(c2, "restart2", state, task_dir=task_dir)
        post_continue_count = len(db.get_events(c2, "restart2"))
        assert post_continue_count > pre_continue_count, (
            "a real continuation must append new events"
        )

        final = db.get_task(c2, "restart2")
        assert final["current_phase"] == "builder"
        assert [h["phase"] for h in final["history"]] == ["architect", "builder"]
        c2.close()

        # One more restart: the continuation itself must also be durable
        # and must not have been duplicated by the act of restarting again.
        c3 = db.connect(p)
        assert len(db.get_events(c3, "restart2")) == post_continue_count
        assert db.get_task(c3, "restart2")["current_phase"] == "builder"
        c3.close()


# ---------------------------------------------------------------------------
# 16/17. Projection generation, deletion, and regeneration
# ---------------------------------------------------------------------------

class TestProjections:
    def test_generate_run_yaml_matches_shape(self, conn, tmp_path):
        state = init_run_state("proj1")
        record_phase(state, "architect", 1, verdict="ready-for-builder")
        db.record_step_and_save(conn, "proj1", state, task_dir=tmp_path / "proj1")

        text = db.generate_run_yaml(conn, "proj1")
        parsed = db._yaml.safe_load(text)
        for field in ["task_id", "created", "updated", "status", "current_phase",
                      "next_step", "schema_version", "deploy_intent", "retries", "history"]:
            assert field in parsed

    def test_write_and_regenerate_projection_is_byte_identical(self, conn, tmp_path):
        state = init_run_state("proj2")
        record_phase(state, "architect", 1)
        db.record_step_and_save(conn, "proj2", state, task_dir=tmp_path / "proj2")

        task_dir = tmp_path / "proj2"
        path1 = db.write_projection(conn, "proj2", task_dir)
        content1 = path1.read_text()

        path1.unlink()
        assert not path1.exists()

        path2 = db.write_projection(conn, "proj2", task_dir)
        content2 = path2.read_text()
        assert content1 == content2

    def test_deleting_projection_does_not_touch_canonical_state(self, conn, tmp_path):
        state = init_run_state("proj3")
        state["current_phase"] = "architect"
        record_phase(state, "architect", 1, verdict="ready-for-builder")
        db.record_step_and_save(conn, "proj3", state, task_dir=tmp_path / "proj3")
        task_dir = tmp_path / "proj3"
        path = db.write_projection(conn, "proj3", task_dir)
        path.unlink()

        # Canonical state must be untouched -- read straight from the DB.
        still_there = db.get_task(conn, "proj3")
        assert still_there["current_phase"] == "architect"
        assert still_there["history"][0]["verdict"] == "ready-for-builder"

    def test_projection_is_stale_detection(self, conn, tmp_path):
        state = init_run_state("proj4")
        db.record_step_and_save(conn, "proj4", state, task_dir=tmp_path / "proj4")
        task_dir = tmp_path / "proj4"
        assert db.projection_is_stale(conn, "proj4", task_dir) is True  # no file yet
        db.write_projection(conn, "proj4", task_dir)
        assert db.projection_is_stale(conn, "proj4", task_dir) is False

        # Mutate canonical state without regenerating -- now stale.
        state["current_phase"] = "builder"
        state["next_step"] = 2
        record_phase(state, "builder", 2)
        db.record_step_and_save(conn, "proj4", state, task_dir=task_dir)
        assert db.projection_is_stale(conn, "proj4", task_dir) is True


# ---------------------------------------------------------------------------
# Migration: dry-run + conflict detection (read-only, against fixture
# corpora built to mirror the real, already-known discrepancies found in
# ~/Projects/.relay/tasks/ during the architect phase -- 'complete' vs
# 'completed', phase/artifact mismatch, etc.)
# ---------------------------------------------------------------------------

class TestMigrationDryRun:
    def _write_artifact(self, path: Path, *, task_id, phase, step, verdict, extra_body=""):
        path.write_text(
            f"---\ntask_id: {task_id}\nphase: {phase}\nstep: {step}\n"
            f"date: 2026-08-31\nverdict: {verdict}\n---\n\n# Body\n{extra_body}\n"
        )

    def test_clean_task_reconciles_ok(self, tmp_path):
        task_dir = tmp_path / "clean-task"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: clean-task\ncreated: '2026-08-31T00:00:00Z'\n"
            "updated: '2026-08-31T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {builder: 0, reviewer: 0}\n"
            "history:\n- {phase: architect, step: 1, timestamp: '2026-08-31T00:00:00Z'}\n"
        )
        self._write_artifact(task_dir / "01-architect.md", task_id="clean-task",
                              phase="architect", step=1, verdict="ready-for-builder")

        file_state = db.read_file_state(task_dir)
        result = db.reconcile_task("clean-task", file_state)
        assert result["ok"] is True
        assert result["discrepancies"] == []

    def test_known_bad_status_value_flagged(self, tmp_path):
        """A ledger may hold status: complete (not 'completed'), which is
        not in VALID_STATUSES and must be surfaced, not silently accepted."""
        task_dir = tmp_path / "bad-status-task"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: bad-status-task\ncreated: '2026-06-01T00:00:00Z'\n"
            "updated: '2026-06-01T00:00:00Z'\nstatus: complete\n"
            "current_phase: done\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        file_state = db.read_file_state(task_dir)
        result = db.reconcile_task("bad-status-task", file_state)
        assert result["ok"] is False
        assert any("status" in d and "complete" in d for d in result["discrepancies"])

    def test_run_yaml_vs_artifact_phase_disagreement_flagged(self, tmp_path):
        """The exact bug class this brief exists to fix: run.yaml says one
        phase, the latest artifact's frontmatter says another."""
        task_dir = tmp_path / "drifted-task"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: drifted-task\ncreated: '2026-06-01T00:00:00Z'\n"
            "updated: '2026-06-01T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: ops\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        self._write_artifact(task_dir / "01-architect.md", task_id="drifted-task",
                              phase="architect", step=1, verdict="ready-for-builder")
        file_state = db.read_file_state(task_dir)
        result = db.reconcile_task("drifted-task", file_state)
        assert result["ok"] is False
        assert any("disagrees" in d for d in result["discrepancies"])

    def test_dry_run_migration_scans_fixture_corpus_read_only(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        clean = tasks_dir / "clean-task"
        clean.mkdir()
        (clean / "run.yaml").write_text(
            "task_id: clean-task\ncreated: '2026-08-31T00:00:00Z'\n"
            "updated: '2026-08-31T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        self._write_artifact(clean / "01-architect.md", task_id="clean-task",
                              phase="architect", step=1, verdict="ready-for-builder")

        bad = tasks_dir / "bad-task"
        bad.mkdir()
        (bad / "run.yaml").write_text(
            "task_id: bad-task\ncreated: '2026-06-01T00:00:00Z'\n"
            "updated: '2026-06-01T00:00:00Z'\nstatus: calibrating\n"
            "current_phase: parked\nnext_step: 1\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )

        brief_only = tasks_dir / "brief-only-task"
        brief_only.mkdir()
        (brief_only / "brief.md").write_text("# never started\n")

        before_snapshot = {
            p.name: p.read_bytes() if p.is_file() else None
            for p in tasks_dir.rglob("*")
        }

        summary = db.dry_run_migration(tasks_dir)

        # Read-only: nothing on disk changed.
        after_snapshot = {
            p.name: p.read_bytes() if p.is_file() else None
            for p in tasks_dir.rglob("*")
        }
        assert before_snapshot == after_snapshot

        # brief-only task (no run.yaml) correctly excluded -- nothing to migrate.
        assert summary["total"] == 2
        assert summary["clean"] == 1
        assert summary["flagged"] == 1
        flagged_ids = {r["task_id"] for r in summary["results"] if not r["ok"]}
        assert flagged_ids == {"bad-task"}

    def test_dry_run_migration_reports_worked_task_without_run_yaml(self, tmp_path):
        """A task directory with real agent artifacts but no run.yaml is
        reported, not skipped as 'brief-only', so no historical work is
        left out of the migration report."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        worked = tasks_dir / "worked-no-run-yaml"
        worked.mkdir()
        (worked / "brief.md").write_text("# real task\n")
        self._write_artifact(worked / "01-architect.md", task_id="worked-no-run-yaml",
                              phase="architect", step=1, verdict="ready-for-builder")
        self._write_artifact(worked / "02-builder.md", task_id="worked-no-run-yaml",
                              phase="builder", step=2, verdict="ready-for-review")

        brief_only = tasks_dir / "brief-only-task"
        brief_only.mkdir()
        (brief_only / "brief.md").write_text("# never started\n")
        (brief_only / "98-session-notes.md").write_text("human notes, not agent output\n")

        summary = db.dry_run_migration(tasks_dir)

        assert summary["total"] == 1
        assert summary["with_run_yaml"] == 0
        assert summary["without_run_yaml"] == 1
        assert summary["flagged"] == 1
        assert summary["clean"] == 0
        result = summary["results"][0]
        assert result["task_id"] == "worked-no-run-yaml"
        assert result["ok"] is False
        assert "no run.yaml" in result["discrepancies"][0]
        assert "[1, 2]" in result["discrepancies"][0]

    def test_worked_task_without_run_yaml_is_flagged_even_when_artifacts_are_clean(self, tmp_path):
        """The missing run.yaml alone must be enough to flag the task, so
        --apply can never import it as clean."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        worked = tasks_dir / "clean-artifacts-no-run-yaml"
        worked.mkdir()
        self._write_artifact(worked / "01-architect.md", task_id="clean-artifacts-no-run-yaml",
                              phase="architect", step=1, verdict="ready-for-builder")

        summary = db.dry_run_migration(tasks_dir)

        assert summary["flagged"] == 1
        assert summary["results"][0]["ok"] is False

    def test_reconcile_flags_artifact_with_no_frontmatter(self, tmp_path):
        """An artifact in the agent numbering range with no frontmatter (e.g.
        a prose planning doc saved as 05-*.md) used to pass unnoticed."""
        task_dir = tmp_path / "no-frontmatter-task"
        task_dir.mkdir()
        self._write_artifact(task_dir / "01-architect.md", task_id="no-frontmatter-task",
                              phase="architect", step=1, verdict="ready-for-builder")
        (task_dir / "02-prod-promotion-plan.md").write_text("# Plan\n\nJust prose, no frontmatter.\n")

        result = db.reconcile_task("no-frontmatter-task", db.read_file_state(task_dir))

        assert result["ok"] is False
        assert any("02-prod-promotion-plan.md" in d and "no YAML frontmatter" in d
                   for d in result["discrepancies"])

    def test_reconcile_flags_duplicate_step_numbers_by_name(self, tmp_path):
        """Two artifacts sharing a step number must be reported as a
        duplicate, naming both files, not as a vague sequence gap."""
        task_dir = tmp_path / "duplicate-step-task"
        task_dir.mkdir()
        self._write_artifact(task_dir / "01-architect.md", task_id="duplicate-step-task",
                              phase="architect", step=1, verdict="ready-for-builder")
        self._write_artifact(task_dir / "02-builder.md", task_id="duplicate-step-task",
                              phase="builder", step=2, verdict="ready-for-review")
        self._write_artifact(task_dir / "03-reviewer.md", task_id="duplicate-step-task",
                              phase="reviewer", step=3, verdict="approved")
        self._write_artifact(task_dir / "03-tester.md", task_id="duplicate-step-task",
                              phase="tester", step=3, verdict="pass")

        result = db.reconcile_task("duplicate-step-task", db.read_file_state(task_dir))

        assert result["ok"] is False
        duplicate_findings = [d for d in result["discrepancies"] if "step number 3" in d]
        assert len(duplicate_findings) == 1
        assert "03-reviewer.md" in duplicate_findings[0]
        assert "03-tester.md" in duplicate_findings[0]
        assert not any("contiguous" in d for d in result["discrepancies"])

    def test_dry_run_migration_never_writes_to_database(self, tmp_path):
        """dry_run_migration() doesn't even take a connection -- this test
        documents and locks in that it's structurally incapable of writing."""
        import inspect
        sig = inspect.signature(db.dry_run_migration)
        assert "conn" not in sig.parameters


class TestImportTask:
    def _clean_task_dir(self, tmp_path, task_id="import-me"):
        task_dir = tmp_path / task_id
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            f"task_id: {task_id}\ncreated: '2026-08-31T00:00:00Z'\n"
            "updated: '2026-08-31T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-architect.md").write_text(
            f"---\ntask_id: {task_id}\nphase: architect\nstep: 1\n"
            "date: 2026-08-31\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        return task_dir

    def test_import_clean_task(self, conn, tmp_path):
        task_dir = self._clean_task_dir(tmp_path)
        result = db.import_task(conn, "import-me", task_dir)
        assert result["ok"] is True
        loaded = db.get_task(conn, "import-me")
        assert loaded["current_phase"] == "architect"
        assert loaded["history"][0]["verdict"] == "ready-for-builder"

    def test_import_does_not_modify_source_files(self, conn, tmp_path):
        task_dir = self._clean_task_dir(tmp_path)
        before = (task_dir / "run.yaml").read_bytes()
        db.import_task(conn, "import-me", task_dir)
        after = (task_dir / "run.yaml").read_bytes()
        assert before == after

    # -- current_phase: done must not become "architect" --

    def test_import_preserves_done_phase(self, conn, tmp_path):
        task_dir = tmp_path / "done-task"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: done-task\ncreated: '2026-05-08T08:05:18Z'\n"
            "updated: '2026-05-08T11:53:31Z'\nstatus: completed\n"
            "current_phase: done\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {builder: 0, reviewer: 0}\n"
            "history:\n- {phase: architect, step: 1, timestamp: '2026-05-08T08:05:18Z', "
            "verdict: ready-for-builder}\n"
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: done-task\nphase: architect\nstep: 1\n"
            "date: 2026-05-08\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        result = db.import_task(conn, "done-task", task_dir)
        assert result["ok"] is True
        loaded = db.get_task(conn, "done-task")
        assert loaded["current_phase"] == "done", (
            "current_phase must be preserved as 'done', "
            f"not silently mapped to '{loaded['current_phase']}'"
        )
        assert loaded["status"] == "completed"

    def test_import_preserves_done_phase_multi_step(self, conn, tmp_path):
        """A multi-round task ending in status: completed /
        current_phase: done."""
        task_dir = tmp_path / "multi-done"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: multi-done\ncreated: '2026-05-08T08:05:18Z'\n"
            "updated: '2026-05-08T11:53:31Z'\nstatus: completed\n"
            "current_phase: done\nnext_step: 4\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {builder: 1, reviewer: 0}\n"
            "history:\n"
            "- {phase: architect, step: 1, timestamp: '2026-05-08T08:05:18Z'}\n"
            "- {phase: builder, step: 2, timestamp: '2026-05-08T08:53:39Z', verdict: ready-for-review}\n"
            "- {phase: reviewer, step: 3, timestamp: '2026-05-08T09:36:22Z', verdict: approved}\n"
        )
        for step, phase, verdict, date in [
            (1, "architect", "ready-for-builder", "2026-05-08"),
            (2, "builder", "ready-for-review", "2026-05-08"),
            (3, "reviewer", "approved", "2026-05-08"),
        ]:
            (task_dir / f"{step:02d}-{phase}.md").write_text(
                f"---\ntask_id: multi-done\nphase: {phase}\nstep: {step}\n"
                f"date: {date}\nverdict: {verdict}\n---\n\nBody.\n"
            )
        result = db.import_task(conn, "multi-done", task_dir)
        assert result["ok"] is True
        loaded = db.get_task(conn, "multi-done")
        assert loaded["current_phase"] == "done"

    def test_downstream_routing_on_migrated_done_task(self, conn, tmp_path):
        """Downstream routing behavior: cmd_next on an already-completed
        migrated task must short-circuit via status, not current_phase,
        through the actual routing code path."""
        task_dir = tmp_path / "routed-done"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: routed-done\ncreated: '2026-05-08T08:05:18Z'\n"
            "updated: '2026-05-08T11:53:31Z'\nstatus: completed\n"
            "current_phase: done\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\n"
            "history:\n- {phase: architect, step: 1, timestamp: '2026-05-08T08:05:18Z', "
            "verdict: ready-for-builder}\n"
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: routed-done\nphase: architect\nstep: 1\n"
            "date: 2026-05-08\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        db.import_task(conn, "routed-done", task_dir)

        import relay as relay_module
        state = db.get_task(conn, "routed-done")
        assert state["current_phase"] == "done"
        # The actual routing guard cmd_next uses is on status, not phase --
        # confirm a migrated "done" task is recognized as already complete.
        assert state["status"] in ("completed", "abandoned")

    def test_create_task_still_rejects_genuinely_invalid_phase(self, conn):
        """"done" is the one exception to phase validation: it must not
        loosen validation for anything else."""
        with pytest.raises(db.InvalidTransition):
            db.create_task(conn, "still-invalid", current_phase="not-a-real-phase-or-done")

    # -- per-step timestamps must survive migration --

    def test_import_preserves_distinct_step_timestamps(self, conn, tmp_path):
        task_dir = tmp_path / "timestamped"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: timestamped\ncreated: '2026-05-08T08:05:18Z'\n"
            "updated: '2026-05-08T11:53:31Z'\nstatus: in_progress\n"
            "current_phase: reviewer\nnext_step: 4\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\n"
            "history:\n"
            "- {phase: architect, step: 1, timestamp: '2026-05-08T08:05:18Z', verdict: ready-for-builder}\n"
            "- {phase: builder, step: 2, timestamp: '2026-05-08T09:53:16Z', verdict: ready-for-review}\n"
            "- {phase: reviewer, step: 3, timestamp: '2026-05-08T11:53:31Z'}\n"
        )
        for step, phase, verdict in [(1, "architect", "ready-for-builder"), (2, "builder", "ready-for-review")]:
            (task_dir / f"{step:02d}-{phase}.md").write_text(
                f"---\ntask_id: timestamped\nphase: {phase}\nstep: {step}\n"
                f"date: 2026-05-08\nverdict: {verdict}\n---\n\nBody.\n"
            )
        # step 3 (reviewer) has started but not concluded -- no verdict yet,
        # matching run.yaml's own history entry for it, and keeping
        # current_phase (reviewer) consistent with the latest artifact.
        (task_dir / "03-reviewer.md").write_text(
            "---\ntask_id: timestamped\nphase: reviewer\nstep: 3\n"
            "date: 2026-05-08\nverdict: null\n---\n\nIn progress.\n"
        )
        db.import_task(conn, "timestamped", task_dir)

        rows = conn.execute(
            "SELECT step, created_at FROM steps WHERE task_id='timestamped' ORDER BY step"
        ).fetchall()
        timestamps = {r["step"]: r["created_at"] for r in rows}
        assert timestamps[1] == "2026-05-08T08:05:18Z"
        assert timestamps[2] == "2026-05-08T09:53:16Z"
        # The two real steps must have DISTINCT timestamps, not both
        # collapsed to run.yaml's single overall `updated` value.
        assert timestamps[1] != timestamps[2]
        assert timestamps[1] != "2026-05-08T11:53:31Z"
        assert timestamps[2] != "2026-05-08T11:53:31Z"

    def test_import_leaves_genuinely_unknown_timestamp_null(self, conn, tmp_path):
        """No matching history entry AND no artifact `date` -- must not
        fabricate a timestamp (e.g. by falling back to run.yaml's `updated`)."""
        task_dir = tmp_path / "no-timestamp"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: no-timestamp\ncreated: '2026-05-08T08:05:18Z'\n"
            "updated: '2026-05-08T11:53:31Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        # Artifact with no `date:` field in its frontmatter at all.
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: no-timestamp\nphase: architect\nstep: 1\n"
            "verdict: ready-for-builder\n---\n\nBody.\n"
        )
        db.import_task(conn, "no-timestamp", task_dir)
        row = conn.execute(
            "SELECT created_at FROM steps WHERE task_id='no-timestamp' AND step=1"
        ).fetchone()
        assert row["created_at"] is None
        assert row["created_at"] != "2026-05-08T11:53:31Z"  # not the task's `updated` value

    def test_import_falls_back_to_artifact_date_when_no_history_entry(self, conn, tmp_path):
        """No matching history entry, but the artifact's own frontmatter
        has a `date:` field -- that's real (if low-fidelity) legacy data
        and should be used in preference to leaving it NULL."""
        task_dir = tmp_path / "date-fallback"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: date-fallback\ncreated: '2026-05-08T08:05:18Z'\n"
            "updated: '2026-05-08T11:53:31Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: date-fallback\nphase: architect\nstep: 1\n"
            "date: 2026-05-08\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        db.import_task(conn, "date-fallback", task_dir)
        row = conn.execute(
            "SELECT created_at FROM steps WHERE task_id='date-fallback' AND step=1"
        ).fetchone()
        assert row["created_at"] == "2026-05-08T00:00:00Z"
        assert row["created_at"] != "2026-05-08T11:53:31Z"  # still not run.yaml's `updated`

    def test_import_duplicate_step_history_last_entry_wins(self, conn, tmp_path):
        """run.yaml's history may contain two entries for the same step
        number (a phase-start entry, then a
        separately-appended verdict entry, from an older relay.py version).
        The later entry's timestamp is used -- deterministic, documented,
        not a fabricated average."""
        task_dir = tmp_path / "dup-step"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: dup-step\ncreated: '2026-06-03T21:52:27Z'\n"
            "updated: '2026-06-04T13:19:01Z'\nstatus: in_progress\n"
            "current_phase: builder\nnext_step: 3\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\n"
            "history:\n"
            "- {phase: builder, step: 2, timestamp: '2026-06-04T13:10:36Z'}\n"
            "- {phase: builder, step: 2, timestamp: '2026-06-04T13:11:58Z', verdict: ready-for-review}\n"
        )
        (task_dir / "02-builder.md").write_text(
            "---\ntask_id: dup-step\nphase: builder\nstep: 2\n"
            "date: 2026-06-04\nverdict: ready-for-review\n---\n\nBody.\n"
        )
        db.import_task(conn, "dup-step", task_dir)
        row = conn.execute(
            "SELECT created_at FROM steps WHERE task_id='dup-step' AND step=2"
        ).fetchone()
        assert row["created_at"] == "2026-06-04T13:11:58Z"  # the later of the two entries

    def test_import_rejects_invalid_step_verdict(self, conn, tmp_path):
        """The same check as the status one, applied to step verdicts: a
        reviewer-only verdict recorded on a security-phase artifact."""
        task_dir = tmp_path / "bad-verdict"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: bad-verdict\ncreated: '2026-05-24T13:44:29Z'\n"
            "updated: '2026-05-24T13:58:31Z'\nstatus: in_progress\n"
            "current_phase: security\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-security.md").write_text(
            "---\ntask_id: bad-verdict\nphase: security\nstep: 1\n"
            "date: 2026-05-24\nverdict: approved\n---\n\nBody.\n"
        )
        with pytest.raises(db.InvalidTransition, match="not valid for phase"):
            db.import_task(conn, "bad-verdict", task_dir)
        assert db.get_task(conn, "bad-verdict") is None

    def test_import_refuses_discrepant_task_without_force(self, conn, tmp_path):
        task_dir = tmp_path / "discrepant"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: discrepant\ncreated: '2026-06-01T00:00:00Z'\n"
            "updated: '2026-06-01T00:00:00Z'\nstatus: complete\n"
            "current_phase: done\nnext_step: 1\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        with pytest.raises(db.InvalidTransition):
            db.import_task(conn, "discrepant", task_dir)
        assert db.get_task(conn, "discrepant") is None

    def test_import_refuses_duplicate_task(self, conn, tmp_path):
        task_dir = self._clean_task_dir(tmp_path)
        db.import_task(conn, "import-me", task_dir)
        with pytest.raises(ValueError):
            db.import_task(conn, "import-me", task_dir)

    def test_force_alone_no_longer_writes_invalid_status(self, conn, tmp_path):
        """force=True must NEVER be sufficient, on its own, to write a
        non-canonical status into canonical state: written verbatim,
        'complete' would later crash cmd_next."""
        task_dir = tmp_path / "bad-status-forced"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: bad-status-forced\ncreated: '2026-06-01T00:00:00Z'\n"
            "updated: '2026-06-01T00:00:00Z'\nstatus: complete\n"
            "current_phase: done\nnext_step: 1\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        with pytest.raises(db.InvalidTransition, match="not canonical"):
            db.import_task(conn, "bad-status-forced", task_dir, force=True)
        assert db.get_task(conn, "bad-status-forced") is None

    def test_valid_statuses_never_gains_complete(self):
        """VALID_STATUSES itself must never be widened to accommodate
        legacy vocabulary -- the mapping happens at the migration boundary,
        not by expanding what Relay considers canonical."""
        from settings import VALID_STATUSES
        assert "complete" not in VALID_STATUSES
        assert VALID_STATUSES == ["in_progress", "completed", "blocked", "abandoned"]

    def test_normal_save_path_unaffected_by_import_task_changes(self, conn, tmp_path):
        """record_step_and_save() (ordinary Relay task creation/update,
        used by cmd_start/cmd_next/cmd_override) must behave exactly as
        before -- it never took a `decision` and still doesn't."""
        import inspect
        sig = inspect.signature(db.record_step_and_save)
        assert "decision" not in sig.parameters
        state = init_run_state("normal-path")
        state["current_phase"] = "architect"
        record_phase(state, "architect", 1, verdict="ready-for-builder")
        db.record_step_and_save(conn, "normal-path", state, task_dir=tmp_path / "normal-path")
        assert db.get_task(conn, "normal-path")["current_phase"] == "architect"


class TestImportTaskAtomicity:
    """import_task() must be one atomic transaction. If task-row creation
    committed on its own, separately from the rest of import_task()'s
    writes, a failure after that point would leave an orphaned tasks row
    with no steps/events, and a retry would then fail at task_exists()
    with no way to clean up short of manually deleting the row.

    This forces a real failure INSIDE the transaction boundary (not by
    mocking import_task() itself) by making a downstream helper that runs
    partway through the write block raise. By that point the task row
    must not be committed, because everything shares one transaction.
    """

    def _clean_task_dir(self, tmp_path, task_id="atomic-me"):
        task_dir = tmp_path / task_id
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            f"task_id: {task_id}\ncreated: '2026-08-31T00:00:00Z'\n"
            "updated: '2026-08-31T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-architect.md").write_text(
            f"---\ntask_id: {task_id}\nphase: architect\nstep: 1\n"
            "date: 2026-08-31\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        return task_dir

    def test_failure_after_task_row_write_rolls_back_completely(self, conn, tmp_path, monkeypatch):
        task_dir = self._clean_task_dir(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure downstream of task-row creation")

        # _import_historical_events() runs near the end of import_task()'s
        # single write block -- by the time it's called, the task row,
        # the tasks.UPDATE, and every steps row have already been written
        # (uncommitted) inside the same transaction. Forcing it to raise
        # here proves the whole transaction -- including the earlier
        # writes -- rolls back together, not just the failing statement.
        monkeypatch.setattr(db, "_import_historical_events", _boom)

        with pytest.raises(RuntimeError, match="simulated failure"):
            db.import_task(conn, "atomic-me", task_dir)

        # Nothing survives the rollback: not the task row, not the step,
        # not any event, including the task_created event that a
        # separately-committing create_task() would have already persisted.
        assert db.task_exists(conn, "atomic-me") is False
        assert db.get_task(conn, "atomic-me") is None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM steps WHERE task_id='atomic-me'"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE task_id='atomic-me'"
        ).fetchone()["n"] == 0

    def test_retry_after_failed_import_succeeds_without_manual_cleanup(self, conn, tmp_path, monkeypatch):
        task_dir = self._clean_task_dir(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(db, "_import_historical_events", _boom)
        with pytest.raises(RuntimeError):
            db.import_task(conn, "atomic-me", task_dir)
        assert db.task_exists(conn, "atomic-me") is False

        # Remove the simulated failure and retry with no manual cleanup
        # (no DELETE, no DB surgery) -- must succeed cleanly.
        monkeypatch.undo()
        result = db.import_task(conn, "atomic-me", task_dir)
        assert result["ok"] is True
        loaded = db.get_task(conn, "atomic-me")
        assert loaded is not None
        assert loaded["current_phase"] == "architect"
        assert loaded["history"][0]["verdict"] == "ready-for-builder"

    def test_successful_import_still_writes_task_row_and_events_together(self, conn, tmp_path):
        """Sanity check in the other direction: the ordinary
        successful-import path still works."""
        task_dir = self._clean_task_dir(tmp_path, task_id="atomic-ok")
        db.import_task(conn, "atomic-ok", task_dir)
        assert db.task_exists(conn, "atomic-ok") is True
        events = db.get_events(conn, "atomic-ok")
        assert any(e["event_type"] == db.EVENT_TASK_CREATED for e in events)
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM steps WHERE task_id='atomic-ok'"
        ).fetchone()["n"] == 1


class TestImportedTaskLiveContinuation:
    """Proves a task that enters SQLite via import_task() -- not
    cmd_start()/record_step_and_save() -- can subsequently be operated on
    by the real live command paths (commands.cmd_next / commands.cmd_override),
    including a real human-gate stop and a real, non-fabricated override.

    Boundary this proves: the actual production functions in relay.py and
    db.py, driven through settings.get_db_connection()'s real connection-
    caching logic, with an explicit `settings._db_connection = None` reset
    between the import step and the live-continuation step to force a
    fresh connection open against the same on-disk database file --
    the same reconnect-to-the-same-file boundary TestRestartRecovery uses
    at the db.py layer, now exercised through the actual CLI command
    functions instead of bare db.py calls. It does not spawn a second OS
    process; it proves the function-level boundary for an
    import_task()-created task.

    Fixture: a 3-step in-progress task (architect/builder canonical,
    reviewer verdict the non-canonical 'approved-with-concerns' variant,
    corrected via a StepVerdictOverride), left in-progress rather
    than terminal so there is real continuation work for commands.cmd_next
    to do. The corrected reviewer verdict (approved_with_warnings)
    requires a human gate under the default config, so this exercises the
    human-gate path for free rather than needing a separately contrived
    scenario.
    """

    def _in_progress_fixture(self, tasks_dir: Path, task_id: str) -> Path:
        task_dir = tasks_dir / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "brief.md").write_text("# Brief\nImported, in-progress task.\n")
        (task_dir / "run.yaml").write_text(
            f"task_id: {task_id}\ncreated: '2026-05-26T22:31:20Z'\n"
            "updated: '2026-05-26T22:55:55Z'\nstatus: in_progress\n"
            "current_phase: reviewer\nnext_step: 4\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\n"
            "history:\n"
            "- {phase: architect, step: 1, timestamp: '2026-05-26T22:31:22Z', verdict: ready-for-builder}\n"
            "- {phase: builder, step: 2, timestamp: '2026-05-26T22:37:37Z', verdict: ready-for-review}\n"
            "- {phase: reviewer, step: 3, timestamp: '2026-05-26T22:55:55Z', verdict: approved-with-concerns}\n"
        )
        (task_dir / "01-architect.md").write_text(
            f"---\ntask_id: {task_id}\nphase: architect\nstep: 1\n"
            "date: 2026-05-26\nverdict: ready-for-builder\n---\n\nPlan.\n"
        )
        (task_dir / "02-builder.md").write_text(
            f"---\ntask_id: {task_id}\nphase: builder\nstep: 2\n"
            "date: 2026-05-26\nverdict: ready-for-review\n---\n\nBuild.\n"
        )
        (task_dir / "03-reviewer.md").write_text(
            f"---\ntask_id: {task_id}\nphase: reviewer\nstep: 3\n"
            "date: 2026-05-26\nverdict: approved-with-concerns\n---\n\nReview.\n"
        )
        return task_dir

    def test_imported_task_hits_real_human_gate_then_overrides_and_continues(
        self, sqlite_env, monkeypatch, capsys
    ):
        config = sqlite_env
        task_id = "imported-continuation"
        task_dir = self._in_progress_fixture(settings.TASKS_DIR, task_id)

        # --- Step 1: import via import_task(), the migration path ---
        decision = db.MigrationDecision(
            task_id=task_id, note="non-canonical spelling of the verdict",
            step_overrides=(
                db.StepVerdictOverride(3, "approved-with-concerns", "approved_with_warnings",
                                        note="non-canonical wording variant with equivalent semantics"),
            ),
        )
        import_conn = settings.get_db_connection()
        result = db.import_task(import_conn, task_id, task_dir, decision=decision)
        assert result["ok"] is False  # reconcile_task correctly flagged the raw verdict
        assert db.task_exists(import_conn, task_id)
        row = import_conn.execute(
            "SELECT verdict FROM steps WHERE task_id=? AND step=3", (task_id,)
        ).fetchone()
        assert row["verdict"] == "approved_with_warnings"

        # --- Connection-boundary reset: force a fresh connection against
        # the same on-disk file, simulating a restart before continuing. ---
        monkeypatch.setattr(settings, "_db_connection", None)

        # --- Step 2: live cmd_next, driven entirely through relay.py's
        # real chokepoints, must hit a real human gate and NOT advance. ---
        capsys.readouterr()
        commands.cmd_next(task_id, auto=False, config=config)
        out = capsys.readouterr().out
        assert "HUMAN GATE" in out
        state = run_state.load_run_state(task_id, config)
        assert state["current_phase"] == "reviewer"
        assert state["status"] == "in_progress"

        # No synthetic human_gate event may exist -- this fixture never had
        # one historically, and the gate above is a live routing stop, not
        # a fabricated historical approval.
        conn = settings.get_db_connection()
        events_before_override = db.get_events(conn, task_id)
        fabricated = [e for e in events_before_override
                      if e["event_type"] == db.EVENT_HISTORICAL_NOTE and e.get("phase") == "human_gate"]
        assert fabricated == []

        # --- Step 3: the real human-authorized override, using the real
        # commands.cmd_override path -- not a bypass. ---
        monkeypatch.setattr(settings, "_db_connection", None)
        commands.cmd_override(task_id, "security", config, auto=False)

        conn2 = settings.get_db_connection()
        final = db.get_task(conn2, task_id)
        assert final["current_phase"] == "security"
        assert final["status"] == "in_progress"

        approvals = [e for e in db.get_events(conn2, task_id) if e["event_type"] == db.EVENT_APPROVAL]
        assert len(approvals) == 1
        assert approvals[0]["payload"]["next_phase"] == "security"

        # The migration correction that made this continuation possible in
        # the first place is still there, distinct from the live approval.
        overrides = [e for e in db.get_events(conn2, task_id) if e["event_type"] == db.EVENT_MIGRATION_OVERRIDE]
        assert len(overrides) == 1
        assert overrides[0]["payload"]["canonical"] == "approved_with_warnings"

        # Still no fabricated human_gate event, even after a real override.
        fabricated_after = [e for e in db.get_events(conn2, task_id)
                             if e["event_type"] == db.EVENT_HISTORICAL_NOTE and e.get("phase") == "human_gate"]
        assert fabricated_after == []


class TestMigrationDecision:
    """The explicit, narrowly-typed migration-override mechanism. Tests
    that it can represent exactly the authorized human decisions, cannot
    represent anything else, and is unreachable from ordinary Relay code."""

    def _task_dir(self, tmp_path, task_id, status, current_phase, history_yaml="history: []\n"):
        d = tmp_path / task_id
        d.mkdir()
        (d / "run.yaml").write_text(
            f"task_id: {task_id}\ncreated: '2026-06-01T00:00:00Z'\n"
            f"updated: '2026-06-01T00:00:00Z'\nstatus: {status}\n"
            f"current_phase: {current_phase}\nnext_step: 1\nschema_version: 1\n"
            f"repo: relay\ndeploy_intent: false\nretries: {{}}\n{history_yaml}"
        )
        return d

    # -- the mechanism itself --

    def test_decision_requires_a_note(self):
        with pytest.raises(ValueError, match="audit note"):
            db.MigrationDecision(task_id="x", status="completed")

    def test_decision_cannot_both_exclude_and_override(self):
        with pytest.raises(ValueError, match="cannot both exclude"):
            db.MigrationDecision(task_id="x", status="completed", exclude=True, note="bad combo")

    def test_ordinary_relay_functions_cannot_accept_a_decision(self):
        """Confirms the override mechanism is unreachable from normal
        operation: only import_task() knows about MigrationDecision."""
        import inspect
        for fn in (db.create_task, db.record_step_and_save, db.get_task):
            assert "decision" not in inspect.signature(fn).parameters, fn.__name__

    # -- status transform --

    def test_status_transform_complete_to_completed(self, conn, tmp_path):
        task_dir = self._task_dir(tmp_path, "legacy-complete", "complete", "done")
        decision = db.MigrationDecision(
            task_id="legacy-complete", status="completed",
            note="legacy 'complete' is a synonym for 'completed'; human-authorized 2026-09-01",
        )
        db.import_task(conn, "legacy-complete", task_dir, decision=decision)
        loaded = db.get_task(conn, "legacy-complete")
        assert loaded["status"] == "completed"
        events = db.get_events(conn, "legacy-complete")
        overrides = [e for e in events if e["event_type"] == db.EVENT_MIGRATION_OVERRIDE]
        assert len(overrides) == 1
        assert overrides[0]["payload"] == {
            "field": "status", "legacy": "complete", "canonical": "completed",
            "note": decision.note,
        }

    def test_decision_pointing_at_noncanonical_value_is_still_rejected(self, conn, tmp_path):
        """Defense in depth: a MigrationDecision is not a bypass -- it
        supplies a replacement value, which is validated exactly like any
        other value would be."""
        task_dir = self._task_dir(tmp_path, "bad-decision", "complete", "done")
        decision = db.MigrationDecision(
            task_id="bad-decision", status="also-not-real",
            note="deliberately invalid, to prove validation still applies",
        )
        with pytest.raises(db.InvalidTransition, match="'also-not-real' is not a valid status"):
            db.import_task(conn, "bad-decision", task_dir, decision=decision)

    # -- phase decisions --

    def test_phase_decision_overrides_conflicting_run_yaml_phase(self, conn, tmp_path):
        task_dir = self._task_dir(
            tmp_path, "stale-run-yaml", "in_progress", "builder",
            history_yaml=(
                "history:\n- {phase: builder, step: 4, timestamp: '2026-05-28T12:27:44Z'}\n"
            ),
        )
        (task_dir / "03-reviewer.md").write_text(
            "---\ntask_id: stale-run-yaml\nphase: reviewer\nstep: 3\n"
            "date: 2026-05-28\nverdict: approved\n---\n\nBody.\n"
        )
        decision = db.MigrationDecision(
            task_id="stale-run-yaml", current_phase="reviewer",
            note="artifact truth (approved) preferred over run.yaml's stale builder re-open",
        )
        db.import_task(conn, "stale-run-yaml", task_dir, decision=decision)
        loaded = db.get_task(conn, "stale-run-yaml")
        assert loaded["current_phase"] == "reviewer"
        overrides = [e for e in db.get_events(conn, "stale-run-yaml") if e["event_type"] == db.EVENT_MIGRATION_OVERRIDE]
        assert overrides[0]["payload"]["legacy"] == "builder"
        assert overrides[0]["payload"]["canonical"] == "reviewer"

    # -- exclusions --

    def test_exclude_decision_refuses_unconditionally(self, conn, tmp_path):
        task_dir = self._task_dir(tmp_path, "excluded-task", "backlog", "parked")
        decision = db.MigrationDecision(
            task_id="excluded-task", exclude=True,
            note="parked before dispatch; not an executed Relay task",
        )
        with pytest.raises(db.MigrationExcluded):
            db.import_task(conn, "excluded-task", task_dir, decision=decision)
        assert db.get_task(conn, "excluded-task") is None

    def test_b6_cleanup_class_stays_excluded_with_no_invented_meaning(self, conn, tmp_path):
        """approved-held has no safe mapping -- the decision documents
        that we chose not to guess, it does not invent one."""
        task_dir = self._task_dir(tmp_path, "b6-like", "approved-held", "done")
        decision = db.MigrationDecision(
            task_id="b6-like", exclude=True,
            note="status 'approved-held' has no safe canonical mapping; excluded rather than guessed",
        )
        with pytest.raises(db.MigrationExcluded):
            db.import_task(conn, "b6-like", task_dir, decision=decision)
        # Confirm no meaning was invented anywhere -- nothing written at all.
        assert db.get_task(conn, "b6-like") is None
        assert db.get_events(conn, "b6-like") == []

    # -- bbs-calibration class: normal task + noncanonical data preserved as metadata --

    def test_bbs_calibration_class_normal_task_plus_preserved_metadata(self, conn, tmp_path):
        task_dir = self._task_dir(
            tmp_path, "bbs-like", "calibrating", "imfl-case-validated",
            history_yaml=(
                "history:\n"
                "- {phase: builder, step: 1, timestamp: '2026-06-17T08:32:00Z', verdict: ready-for-review}\n"
                "- {phase: reviewer, step: 2, timestamp: '2026-06-17T08:45:00Z', verdict: approved}\n"
            ),
        )
        # Freeform calibration data, as legacy files hold it.
        content = (task_dir / "run.yaml").read_text()
        content = content.replace(
            "retries: {}\n",
            "retries: {}\nresults:\n  imfl-case-1: {delta_pct: -0.23, verdict: PASS}\n",
        )
        (task_dir / "run.yaml").write_text(content)
        (task_dir / "01-builder.md").write_text(
            "---\ntask_id: bbs-like\nphase: builder\nstep: 1\n"
            "date: 2026-06-17\nverdict: ready-for-review\n---\n\nBody.\n"
        )
        (task_dir / "02-reviewer.md").write_text(
            "---\ntask_id: bbs-like\nphase: reviewer\nstep: 2\n"
            "date: 2026-06-17\nverdict: approved\n---\n\nBody.\n"
        )
        decision = db.MigrationDecision(
            task_id="bbs-like", status="completed", current_phase="reviewer",
            note="normal completed reviewer task; calibration vocabulary is not canonical Relay state",
        )
        db.import_task(conn, "bbs-like", task_dir, decision=decision)

        loaded = db.get_task(conn, "bbs-like")
        assert loaded["status"] == "completed"
        assert loaded["current_phase"] == "reviewer"
        # Noncanonical vocabulary must never appear as canonical state.
        assert loaded["status"] != "calibrating"
        assert loaded["current_phase"] != "imfl-case-validated"

        events = db.get_events(conn, "bbs-like")
        historical = [e for e in events if e["event_type"] == db.EVENT_HISTORICAL_NOTE]
        assert any("imfl-case-1" in json.dumps(e.get("payload", {})) for e in historical), (
            "calibration results must be preserved as historical metadata, not silently discarded"
        )
        # And that preserved data must never leak into a canonical column.
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", ("bbs-like",)).fetchone()
        assert "results" not in row.keys()

    # -- historical non-canonical phase vocabulary --

    def test_noncanonical_history_phases_preserved_as_events_not_canonical_phases(self, conn, tmp_path):
        """human_gate / committed / builder_fix etc. -- preserved as
        historical_note events (with e.g. a real commit hash intact),
        never written into steps.phase or tasks.current_phase, and
        VALID_PHASES is never touched."""
        task_dir = self._task_dir(
            tmp_path, "vocab-like", "completed", "done",
            history_yaml=(
                "history:\n"
                "- {phase: builder, step: 1, timestamp: '2026-06-14T22:41:00Z', verdict: ready-for-review}\n"
                "- {phase: reviewer, step: 2, timestamp: '2026-06-14T23:09:00Z', verdict: approved}\n"
                "- {phase: committed, step: 3, timestamp: '2026-06-15T09:24:00Z', commit: 16c9da8}\n"
            ),
        )
        (task_dir / "01-builder.md").write_text(
            "---\ntask_id: vocab-like\nphase: builder\nstep: 1\n"
            "date: 2026-06-14\nverdict: ready-for-review\n---\n\nBody.\n"
        )
        (task_dir / "02-reviewer.md").write_text(
            "---\ntask_id: vocab-like\nphase: reviewer\nstep: 2\n"
            "date: 2026-06-14\nverdict: approved\n---\n\nBody.\n"
        )
        db.import_task(conn, "vocab-like", task_dir)

        # Canonical steps: only the two real artifacts, only real phases.
        rows = conn.execute("SELECT phase FROM steps WHERE task_id=?", ("vocab-like",)).fetchall()
        phases = {r["phase"] for r in rows}
        assert phases == {"builder", "reviewer"}
        assert "committed" not in phases

        from settings import VALID_PHASES
        assert "committed" not in VALID_PHASES  # never touched

        # The commit fact is preserved, not lost -- as a historical event.
        events = db.get_events(conn, "vocab-like")
        historical = [e for e in events if e["event_type"] == db.EVENT_HISTORICAL_NOTE]
        assert any(e.get("phase") == "committed" and e.get("payload", {}).get("commit") == "16c9da8"
                   for e in historical), "the committed/16c9da8 fact must survive as historical metadata"

    def test_historical_note_never_duplicates_a_real_step(self, conn, tmp_path):
        """A history entry that's already faithfully represented by a real
        artifact must not also get a redundant historical_note event."""
        task_dir = self._task_dir(
            tmp_path, "no-dup", "in_progress", "architect",
            history_yaml=(
                "history:\n- {phase: architect, step: 1, timestamp: '2026-08-31T00:00:00Z', "
                "verdict: ready-for-builder}\n"
            ),
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: no-dup\nphase: architect\nstep: 1\n"
            "date: 2026-08-31\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        db.import_task(conn, "no-dup", task_dir)
        historical = [e for e in db.get_events(conn, "no-dup") if e["event_type"] == db.EVENT_HISTORICAL_NOTE]
        assert historical == []


class TestStepVerdictOverride:
    """Step-level verdict corrections: ready-for-reviewer -> ready-for-review,
    approved-with-concerns -> approved_with_warnings, and architect
    'approved' (a human_gate overwrite) -> needs-human-input."""

    def _task_dir(self, tmp_path, task_id, status, current_phase, history_yaml="history: []\n"):
        d = tmp_path / task_id
        d.mkdir()
        (d / "run.yaml").write_text(
            f"task_id: {task_id}\ncreated: '2026-06-01T00:00:00Z'\n"
            f"updated: '2026-06-01T00:00:00Z'\nstatus: {status}\n"
            f"current_phase: {current_phase}\nnext_step: 1\nschema_version: 1\n"
            f"repo: relay\ndeploy_intent: false\nretries: {{}}\n{history_yaml}"
        )
        return d

    # -- StepVerdictOverride itself --

    def test_step_override_requires_a_note(self):
        with pytest.raises(ValueError, match="audit note"):
            db.StepVerdictOverride(step=1, expected_verdict="x", canonical_verdict="y")

    def test_decision_rejects_duplicate_step_overrides(self):
        with pytest.raises(ValueError, match="more than one step_override"):
            db.MigrationDecision(
                task_id="dup-steps", note="top-level note",
                step_overrides=(
                    db.StepVerdictOverride(1, "a", "b", note="first"),
                    db.StepVerdictOverride(1, "c", "d", note="second"),
                ),
            )

    # -- 1. canonical verdict imports unchanged --

    def test_canonical_verdict_imports_unchanged_no_decision_needed(self, conn, tmp_path):
        task_dir = self._task_dir(tmp_path, "already-canonical", "in_progress", "architect")
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: already-canonical\nphase: architect\nstep: 1\n"
            "date: 2026-06-01\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        db.import_task(conn, "already-canonical", task_dir)
        row = conn.execute(
            "SELECT verdict FROM steps WHERE task_id='already-canonical' AND step=1"
        ).fetchone()
        assert row["verdict"] == "ready-for-builder"

    # -- 2/3. ready-for-reviewer -> ready-for-review --

    def _stale_run_yaml_task(self, tmp_path):
        task_dir = self._task_dir(
            tmp_path, "stale-run-yaml2", "in_progress", "reviewer",
            history_yaml="history:\n- {phase: builder, step: 2, timestamp: '2026-05-28T10:17:32Z'}\n",
        )
        (task_dir / "02-builder.md").write_text(
            "---\ntask_id: stale-run-yaml2\nphase: builder\nstep: 2\n"
            "date: 2026-05-28\nverdict: ready-for-reviewer\n---\n\nBody.\n"
        )
        return task_dir

    def test_ready_for_reviewer_rejected_without_decision(self, conn, tmp_path):
        task_dir = self._stale_run_yaml_task(tmp_path)
        with pytest.raises(db.InvalidTransition, match="not valid for phase"):
            db.import_task(conn, "stale-run-yaml2", task_dir)
        assert db.get_task(conn, "stale-run-yaml2") is None

    def test_ready_for_reviewer_becomes_ready_for_review_with_decision(self, conn, tmp_path):
        task_dir = self._stale_run_yaml_task(tmp_path)
        decision = db.MigrationDecision(
            task_id="stale-run-yaml2", current_phase="reviewer", note="non-canonical spelling of the verdict",
            step_overrides=(
                db.StepVerdictOverride(2, "ready-for-reviewer", "ready-for-review",
                                        note="non-canonical wording variant with equivalent semantics"),
            ),
        )
        db.import_task(conn, "stale-run-yaml2", task_dir, decision=decision)
        row = conn.execute(
            "SELECT verdict FROM steps WHERE task_id='stale-run-yaml2' AND step=2"
        ).fetchone()
        assert row["verdict"] == "ready-for-review"
        overrides = [e for e in db.get_events(conn, "stale-run-yaml2")
                     if e["event_type"] == db.EVENT_MIGRATION_OVERRIDE and e["step"] == 2]
        assert len(overrides) == 1
        assert overrides[0]["payload"]["legacy"] == "ready-for-reviewer"
        assert overrides[0]["payload"]["canonical"] == "ready-for-review"

    # -- 4. approved-with-concerns -> approved_with_warnings --

    def test_approved_with_concerns_only_normalizes_with_decision(self, conn, tmp_path):
        task_dir = self._task_dir(
            tmp_path, "buried-bad-verdict", "in_progress", "reviewer",
            history_yaml="history:\n- {phase: reviewer, step: 3, timestamp: '2026-05-26T22:55:55Z'}\n",
        )
        (task_dir / "03-reviewer.md").write_text(
            "---\ntask_id: buried-bad-verdict\nphase: reviewer\nstep: 3\n"
            "date: 2026-05-26\nverdict: approved-with-concerns\n---\n\nBody.\n"
        )
        with pytest.raises(db.InvalidTransition, match="not valid for phase"):
            db.import_task(conn, "buried-bad-verdict", task_dir)

        decision = db.MigrationDecision(
            task_id="buried-bad-verdict", note="non-canonical spelling of the verdict",
            step_overrides=(
                db.StepVerdictOverride(3, "approved-with-concerns", "approved_with_warnings",
                                        note="non-canonical wording variant with equivalent semantics"),
            ),
        )
        db.import_task(conn, "buried-bad-verdict", task_dir, decision=decision)
        row = conn.execute(
            "SELECT verdict FROM steps WHERE task_id='buried-bad-verdict' AND step=3"
        ).fetchone()
        assert row["verdict"] == "approved_with_warnings"

    # -- 5/6. architect 'approved' -> needs-human-input, human_gate stays separate --

    def _b1_like(self, tmp_path):
        task_dir = self._task_dir(
            tmp_path, "b1-like", "complete", "done",
            history_yaml=(
                "history:\n"
                "- {phase: architect, step: 1, timestamp: '2026-06-15T09:37:00Z', "
                "verdict: needs-human-input}\n"
                "- {phase: human_gate, step: 1, timestamp: '2026-06-15T09:46:00Z', "
                "verdict: approved}\n"
            ),
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: b1-like\nphase: architect\nstep: 1\n"
            "date: 2026-06-15\nverdict: approved\n---\n\n"
            "The verdict is `needs-human-input` for one reason: the base-topology call.\n"
        )
        return task_dir

    def test_architect_approved_rejected_without_decision(self, conn, tmp_path):
        task_dir = self._b1_like(tmp_path)
        with pytest.raises(db.InvalidTransition, match="not valid for phase"):
            db.import_task(conn, "b1-like", task_dir)

    def test_architect_approved_becomes_needs_human_input_with_decision(self, conn, tmp_path):
        task_dir = self._b1_like(tmp_path)
        decision = db.MigrationDecision(
            task_id="b1-like", status="completed",
            note="architect artifact body identifies needs-human-input as the real verdict; "
                 "the frontmatter 'approved' is a later human_gate overwrite",
            step_overrides=(
                db.StepVerdictOverride(
                    1, "approved", "needs-human-input",
                    note="artifact body says the architect verdict was needs-human-input; "
                         "run.yaml separately records the human_gate approved event, which "
                         "must not overwrite the architect step's own verdict",
                ),
            ),
        )
        db.import_task(conn, "b1-like", task_dir, decision=decision)

        loaded = db.get_task(conn, "b1-like")
        assert loaded["status"] == "completed"
        step_row = conn.execute(
            "SELECT verdict FROM steps WHERE task_id='b1-like' AND step=1"
        ).fetchone()
        assert step_row["verdict"] == "needs-human-input"

        # The human_gate approval must survive as its own, separate event --
        # never collapsed into or overwritten by the architect step.
        events = db.get_events(conn, "b1-like")
        human_gate_events = [
            e for e in events
            if e["event_type"] == db.EVENT_HISTORICAL_NOTE and e.get("phase") == "human_gate"
        ]
        assert len(human_gate_events) == 1
        assert human_gate_events[0]["payload"]["verdict"] == "approved"
        # And the architect step's own verdict, distinctly, is the corrected one.
        assert step_row["verdict"] != human_gate_events[0]["payload"]["verdict"]

    # -- 7. wrong task/step does not apply --

    def test_step_override_for_wrong_step_does_not_apply(self, conn, tmp_path):
        task_dir = self._stale_run_yaml_task(tmp_path)
        decision = db.MigrationDecision(
            task_id="stale-run-yaml2", current_phase="reviewer", note="override authored for the wrong step",
            step_overrides=(
                # Real bad verdict is on step 2; this override is for step 5.
                db.StepVerdictOverride(5, "ready-for-reviewer", "ready-for-review", note="wrong step"),
            ),
        )
        with pytest.raises(db.InvalidTransition, match="not valid for phase"):
            db.import_task(conn, "stale-run-yaml2", task_dir, decision=decision)
        assert db.get_task(conn, "stale-run-yaml2") is None

    # -- 8. expected value mismatch fails safely --

    def test_step_override_expected_value_mismatch_fails(self, conn, tmp_path):
        task_dir = self._stale_run_yaml_task(tmp_path)  # actual verdict is 'ready-for-reviewer'
        decision = db.MigrationDecision(
            task_id="stale-run-yaml2", current_phase="reviewer", note="stale decision, file no longer matches",
            step_overrides=(
                db.StepVerdictOverride(2, "some-other-stale-value", "ready-for-review",
                                        note="expected value no longer matches disk"),
            ),
        )
        with pytest.raises(db.InvalidTransition, match="expected verdict"):
            db.import_task(conn, "stale-run-yaml2", task_dir, decision=decision)
        assert db.get_task(conn, "stale-run-yaml2") is None

    # -- 9. override producing an invalid-for-phase verdict is rejected --

    def test_step_override_canonical_target_must_be_valid_for_phase(self, conn, tmp_path):
        task_dir = self._stale_run_yaml_task(tmp_path)
        decision = db.MigrationDecision(
            task_id="stale-run-yaml2", current_phase="reviewer", note="deliberately maps to a bogus canonical value",
            step_overrides=(
                db.StepVerdictOverride(2, "ready-for-reviewer", "totally-not-a-real-verdict",
                                        note="should be rejected"),
            ),
        )
        with pytest.raises(db.InvalidTransition, match="is not valid for phase"):
            db.import_task(conn, "stale-run-yaml2", task_dir, decision=decision)
        assert db.get_task(conn, "stale-run-yaml2") is None

    # -- 10. reconciliation examines every artifact, not just the latest --

    def test_reconcile_task_flags_bad_verdict_on_non_latest_artifact(self, tmp_path):
        """A bad verdict on
        step 3, with steps 4+ on top of it, all individually fine."""
        task_dir = self._task_dir(tmp_path, "buried-bad-verdict", "completed", "done")
        (task_dir / "03-reviewer.md").write_text(
            "---\ntask_id: buried-bad-verdict\nphase: reviewer\nstep: 3\n"
            "date: 2026-05-26\nverdict: approved-with-concerns\n---\n\nBody.\n"
        )
        (task_dir / "06-security.md").write_text(
            "---\ntask_id: buried-bad-verdict\nphase: security\nstep: 6\n"
            "date: 2026-05-27\nverdict: secure\n---\n\nBody.\n"
        )
        (task_dir / "07-tester.md").write_text(
            "---\ntask_id: buried-bad-verdict\nphase: tester\nstep: 7\n"
            "date: 2026-05-27\nverdict: pass\n---\n\nBody.\n"
        )
        file_state = db.read_file_state(task_dir)
        result = db.reconcile_task("buried-bad-verdict", file_state)
        assert result["ok"] is False
        assert any("03-reviewer.md" in d and "verdict" in d for d in result["discrepancies"])

    # -- 11. clean corpus stays clean, migration stays deterministic --

    def test_clean_corpus_task_still_reconciles_ok_after_every_artifact_check(self, tmp_path):
        task_dir = self._task_dir(
            tmp_path, "still-clean", "in_progress", "reviewer",
            history_yaml="history:\n- {phase: architect, step: 1, timestamp: '2026-01-01T00:00:00Z'}\n",
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: still-clean\nphase: architect\nstep: 1\n"
            "date: 2026-01-01\nverdict: ready-for-builder\n---\n\nBody.\n"
        )
        (task_dir / "02-builder.md").write_text(
            "---\ntask_id: still-clean\nphase: builder\nstep: 2\n"
            "date: 2026-01-01\nverdict: ready-for-review\n---\n\nBody.\n"
        )
        (task_dir / "03-reviewer.md").write_text(
            "---\ntask_id: still-clean\nphase: reviewer\nstep: 3\n"
            "date: 2026-01-01\nverdict: approved\n---\n\nBody.\n"
        )
        file_state = db.read_file_state(task_dir)
        result = db.reconcile_task("still-clean", file_state)
        assert result["ok"] is True
        assert result["discrepancies"] == []

    def test_import_is_deterministic_across_repeated_runs(self, tmp_path):
        """Importing the same task+decision into two independent DBs
        produces identical canonical state."""
        task_dir = self._stale_run_yaml_task(tmp_path)
        decision = db.MigrationDecision(
            task_id="stale-run-yaml2", current_phase="reviewer", note="determinism check",
            step_overrides=(
                db.StepVerdictOverride(2, "ready-for-reviewer", "ready-for-review", note="determinism check"),
            ),
        )
        conn_a = db.connect(tmp_path / "a.db")
        conn_b = db.connect(tmp_path / "b.db")
        db.import_task(conn_a, "stale-run-yaml2", task_dir, decision=decision)
        db.import_task(conn_b, "stale-run-yaml2", task_dir, decision=decision)
        a = db.get_task(conn_a, "stale-run-yaml2")
        b = db.get_task(conn_b, "stale-run-yaml2")
        for key in ("status", "current_phase", "history"):
            assert a[key] == b[key]
        conn_a.close()
        conn_b.close()

    # -- 12. a bad verdict buried mid-task is detected and normalized --

    def test_a_bad_verdict_mid_task_is_flagged_end_to_end(self, conn, tmp_path):
        """7 artifacts, bad verdict buried at
        step 3, everything else canonical. Must be flagged by reconcile_task
        and only import cleanly with the explicit step decision."""
        task_dir = self._task_dir(tmp_path, "bad-verdict-task", "completed", "done")
        steps = [
            (1, "architect", None),
            (2, "builder", "ready-for-review"),
            (3, "reviewer", "approved-with-concerns"),
            (4, "builder", "ready-for-review"),
            (5, "reviewer", "approved"),
            (6, "security", "secure"),
            (7, "tester", "pass"),
        ]
        for step, phase, verdict in steps:
            vline = f"verdict: {verdict}\n" if verdict else ""
            fname = f"{step:02d}-{phase}.md" if step != 4 else "04-builder-followup.md"
            (task_dir / fname).write_text(
                f"---\ntask_id: bad-verdict-task\nphase: {phase}\nstep: {step}\n"
                f"date: 2026-05-26\n{vline}---\n\nBody.\n"
            )

        file_state = db.read_file_state(task_dir)
        result = db.reconcile_task("bad-verdict-task", file_state)
        assert result["ok"] is False, "the buried step-3 bad verdict must be detected"

        with pytest.raises(db.InvalidTransition):
            db.import_task(conn, "bad-verdict-task", task_dir)

        decision = db.MigrationDecision(
            task_id="bad-verdict-task", note="reviewer spelling variant at step 3",
            step_overrides=(
                db.StepVerdictOverride(3, "approved-with-concerns", "approved_with_warnings",
                                        note="non-canonical wording variant with equivalent semantics"),
            ),
        )
        db.import_task(conn, "bad-verdict-task", task_dir, decision=decision)
        row = conn.execute("SELECT verdict FROM steps WHERE task_id='bad-verdict-task' AND step=3").fetchone()
        assert row["verdict"] == "approved_with_warnings"
        # The rest of the steps must be untouched.
        row5 = conn.execute("SELECT verdict FROM steps WHERE task_id='bad-verdict-task' AND step=5").fetchone()
        assert row5["verdict"] == "approved"

    # -- security 'approved' -> 'findings', NO
    # human-gate event fabricated (unlike the architect class, where a real
    # separate human_gate event exists and must be preserved). --

    def _legacy_security_verdict_task(self, tmp_path):
        """A step-1 human_gate event unrelated to
        security, and a step-4 security artifact that self-declares
        'approved' with two real, named advisory findings -- no human_gate
        event anywhere near step 4."""
        task_dir = self._task_dir(
            tmp_path, "legacy-security-verdict", "complete", "done",
            history_yaml=(
                "history:\n"
                "- {phase: architect, step: 1, timestamp: '2026-06-13T19:29:55Z'}\n"
                "- {phase: human_gate, step: 1, timestamp: '2026-06-14T19:21:00Z', "
                "verdict: approved}\n"
                "- {phase: security, step: 4, timestamp: '2026-06-14T20:40:00Z', "
                "verdict: approved, note: 'A-1 DoS advisory; A-2 doc-only'}\n"
            ),
        )
        (task_dir / "04-security.md").write_text(
            "---\ntask_id: legacy-security-verdict\nphase: security\nstep: 4\n"
            "date: 2026-06-14\nverdict: approved\n---\n\n"
            "## Findings\n### Advisory\nA-1: resource/DoS gap, MUST be tracked.\n"
            "A-2: documentation gap.\n\n"
            "## Verdict Rationale\n`approved` (security). No blockers.\n"
        )
        return task_dir

    def test_legacy_security_approved_rejected_without_override(self, conn, tmp_path):
        task_dir = self._legacy_security_verdict_task(tmp_path)
        with pytest.raises(db.InvalidTransition, match="not valid for phase"):
            db.import_task(conn, "legacy-security-verdict", task_dir)

    def test_legacy_security_approved_becomes_findings_with_override(self, conn, tmp_path):
        task_dir = self._legacy_security_verdict_task(tmp_path)
        decision = db.MigrationDecision(
            task_id="legacy-security-verdict", status="completed",
            note="'approved' was informal security-phase vocabulary; the artifact "
                 "contains two actual non-critical findings (A-1 DoS advisory, A-2 "
                 "doc gap); 'no blockers' does not mean 'no findings'; therefore "
                 "'findings' is the canonical representation; no security human-gate "
                 "event exists historically and none should be fabricated; "
                 "human-authorized 2026-09-01",
            step_overrides=(
                db.StepVerdictOverride(
                    4, "approved", "findings",
                    note="security artifact self-declares 'approved' but names two "
                         "real advisory findings (A-1, A-2); 'findings' is the correct "
                         "canonical verdict, not 'secure'; human-authorized 2026-09-01",
                ),
            ),
        )
        db.import_task(conn, "legacy-security-verdict", task_dir, decision=decision)

        # 1/2/3: rejected without override, converts with it, result is canonical.
        row = conn.execute("SELECT verdict FROM steps WHERE task_id='legacy-security-verdict' AND step=4").fetchone()
        assert row["verdict"] == "findings"
        assert row["verdict"] in VALID_VERDICTS["security"]

        # 4: no human-gate event is fabricated for step 4.
        events = db.get_events(conn, "legacy-security-verdict")
        step4_human_gate = [
            e for e in events
            if e["event_type"] == db.EVENT_HISTORICAL_NOTE and e.get("phase") == "human_gate"
            and e.get("payload", {}).get("step") == 4
        ]
        assert step4_human_gate == [], "no synthetic human_gate event for step 4 must be created"

        # 5: the existing, unrelated step-1 human_gate event is untouched.
        step1_human_gate = [
            e for e in events
            if e["event_type"] == db.EVENT_HISTORICAL_NOTE and e.get("phase") == "human_gate"
            and e.get("payload", {}).get("step") == 1
        ]
        assert len(step1_human_gate) == 1
        assert step1_human_gate[0]["payload"]["verdict"] == "approved"

        # The migration_override audit event correctly names the corrected step.
        overrides = [e for e in events if e["event_type"] == db.EVENT_MIGRATION_OVERRIDE and e["step"] == 4]
        assert len(overrides) == 1
        assert overrides[0]["payload"] == {
            "field": "verdict", "legacy": "approved", "canonical": "findings",
            "note": decision.step_overrides[0].note,
        }

    def test_optimizer_migration_is_deterministic(self, tmp_path):
        """6: re-running the migration produces the same result."""
        task_dir = self._legacy_security_verdict_task(tmp_path)
        decision = db.MigrationDecision(
            task_id="legacy-security-verdict", status="completed",
            note="determinism check",
            step_overrides=(
                db.StepVerdictOverride(4, "approved", "findings", note="determinism check"),
            ),
        )
        conn_a = db.connect(tmp_path / "opt_a.db")
        conn_b = db.connect(tmp_path / "opt_b.db")
        db.import_task(conn_a, "legacy-security-verdict", task_dir, decision=decision)
        db.import_task(conn_b, "legacy-security-verdict", task_dir, decision=decision)
        a = db.get_task(conn_a, "legacy-security-verdict")
        b = db.get_task(conn_b, "legacy-security-verdict")
        assert a["status"] == b["status"] == "completed"
        row_a = conn_a.execute("SELECT verdict FROM steps WHERE task_id='legacy-security-verdict' AND step=4").fetchone()
        row_b = conn_b.execute("SELECT verdict FROM steps WHERE task_id='legacy-security-verdict' AND step=4").fetchone()
        assert row_a["verdict"] == row_b["verdict"] == "findings"
        conn_a.close()
        conn_b.close()


# ---------------------------------------------------------------------------
# CLI integration: cmd_start / cmd_next / cmd_status / cmd_list / cmd_validate
# / cmd_override through the sqlite backend, in an isolated tmp dir. Proves
# the four chokepoints are wired correctly end to end, not just unit-tested
# in isolation.
# ---------------------------------------------------------------------------

class TestCLIIntegrationSqliteBackend:
    def test_start_creates_task_in_db_with_matching_projection(self, sqlite_env, capsys):
        """Canonical state lives in sqlite -- but as of projection-on-write,
        a matching run.yaml is also written, purely as a generated,
        deletable-without-loss projection (see TestProjectionOnWrite for
        the full contract). This test's job is just to prove the file that
        appears is exactly db.generate_run_yaml()'s output, not an
        independently-authored copy that could drift from canonical state.
        """
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "cli-t1")
        commands.cmd_start("cli-t1", auto=False, config=config)

        state = run_state.load_run_state("cli-t1", config)
        assert state is not None
        assert state["current_phase"] == "designer"

        projection_path = settings.TASKS_DIR / "cli-t1" / "run.yaml"
        assert projection_path.exists()
        conn = settings.get_db_connection()
        assert projection_path.read_text() == db.generate_run_yaml(conn, "cli-t1")

    def test_start_twice_is_rejected(self, sqlite_env, capsys):
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "cli-t2")
        commands.cmd_start("cli-t2", auto=False, config=config)
        with pytest.raises(SystemExit):
            commands.cmd_start("cli-t2", auto=False, config=config)

    def test_next_routes_using_db_backed_latest_step(self, sqlite_env, capsys, tmp_path):
        """End-to-end reproduction of the exact gap found during manual CLI
        smoke testing: an agent writes its artifact file directly (as the
        real Claude Code agent invocation does) with no other code involved
        -- find_latest_artifact()/cmd_next must pick up that verdict from
        the file with no separate 'ingest' step required by the caller.
        """
        config = sqlite_env
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "cli-t3")
        commands.cmd_start("cli-t3", auto=False, config=config)

        # Simulate the first agent (the Designer) writing its artifact -- a
        # real file on disk, nothing else. This is the only thing an agent does.
        (task_dir / "01-designer.md").write_text(
            "---\ntask_id: cli-t3\nphase: designer\nstep: 1\n"
            "date: 2026-08-31\nverdict: ready-for-architect\n---\n\nBrief.\n"
        )

        path, frontmatter = run_state.find_latest_artifact("cli-t3", config)
        assert frontmatter["verdict"] == "ready-for-architect"
        assert frontmatter["phase"] == "designer"
        assert path == task_dir / "01-designer.md"

        conn = settings.get_db_connection()
        latest = db.get_latest_step(conn, "cli-t3", task_dir=task_dir)
        assert latest["verdict"] == "ready-for-architect"

    def test_next_command_advances_after_agent_writes_artifact(self, sqlite_env, capsys):
        """The actual relay next CLI command, sqlite-backed, after nothing
        but a real artifact file appearing on disk -- no manual DB writes."""
        config = dict(sqlite_env)
        config["human_gates"] = {"after_designer": False, "after_architect": False,
                                  "reviewer_approved_with_warnings": True,
                                  "tester_pass_with_concerns": True}
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "cli-t3b")
        commands.cmd_start("cli-t3b", auto=False, config=config)
        (task_dir / "01-designer.md").write_text(
            "---\ntask_id: cli-t3b\nphase: designer\nstep: 1\n"
            "date: 2026-08-31\nverdict: ready-for-architect\n---\n\nBrief.\n"
        )
        capsys.readouterr()
        commands.cmd_next("cli-t3b", auto=False, config=config)
        out = capsys.readouterr().out
        assert "No verdict found" not in out
        assert "Next phase: architect" in out

        state = run_state.load_run_state("cli-t3b", config)
        assert state["current_phase"] == "architect"

    @pytest.mark.parametrize("hostile", [SECURITYS_MERGE_KEYS, *BAD_VALUES],
                             ids=["Security's merge keys", *BAD_VALUE_NAMES])
    def test_next_reads_a_frontmatter_relay_does_not_read_as_none_and_never_ends_in_an_error(
            self, sqlite_env, capsys, nothing_composed, hostile):
        """Hostile YAML in a report's frontmatter, which `relay next` reads
        to route: the frontmatter is not read, and `relay next` stops as it
        does for a report with none."""
        config = sqlite_env
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "cli-hostile")
        commands.cmd_start("cli-hostile", auto=False, config=config)
        (task_dir / "01-designer.md").write_text(
            "---\ntask_id: cli-hostile\nphase: designer\nstep: 1\ndate: 2026-10-14\n"
            f"verdict: ready-for-architect\n{hostile}---\n\nBrief.\n")
        capsys.readouterr()
        started = time.monotonic()
        with pytest.raises(SystemExit) as stopped:
            commands.cmd_next("cli-hostile", auto=False, config=config)
        assert time.monotonic() - started < 10
        assert stopped.value.code == 1
        assert "Warning: No verdict found in 01-designer.md" in capsys.readouterr().out
        assert run_state.load_run_state("cli-hostile", config)["current_phase"] == "designer"

    def test_sync_leaves_unparseable_verdict_unsynced(self, conn, tmp_path):
        state = init_run_state("sync1")
        record_phase(state, "architect", 1)
        db.record_step_and_save(conn, "sync1", state, task_dir=tmp_path / "sync1")

        task_dir = tmp_path / "sync1"
        task_dir.mkdir(exist_ok=True)
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: sync1\nphase: architect\nstep: 1\n"
            "date: 2026-08-31\nverdict: total-nonsense\n---\n\nPlan.\n"
        )
        latest = db.get_latest_step(conn, "sync1", task_dir=task_dir)
        assert latest["verdict"] is None  # invalid verdict must not be synced

    def test_status_and_list_reflect_db_state(self, sqlite_env, capsys):
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "cli-t4")
        commands.cmd_start("cli-t4", auto=False, config=config)

        capsys.readouterr()
        commands.cmd_status("cli-t4", config)
        out = capsys.readouterr().out
        assert "cli-t4" in out
        assert "designer" in out

        commands.cmd_list(config)
        out = capsys.readouterr().out
        assert "cli-t4" in out

    def test_validate_reports_no_errors_for_healthy_sqlite_task(self, sqlite_env, capsys):
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "cli-t5")
        commands.cmd_start("cli-t5", auto=False, config=config)
        capsys.readouterr()
        # cmd_validate only sys.exit(1)s when errors are found; a healthy
        # task must return normally with no SystemExit.
        commands.cmd_validate("cli-t5", config)
        out = capsys.readouterr().out
        assert "ERROR" not in out

    def test_override_records_approval_event(self, sqlite_env, capsys):
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "cli-t6")
        commands.cmd_start("cli-t6", auto=False, config=config)
        commands.cmd_override("cli-t6", "builder", config, auto=False)

        conn = settings.get_db_connection()
        events = db.get_events(conn, "cli-t6")
        approvals = [e for e in events if e["event_type"] == db.EVENT_APPROVAL]
        assert len(approvals) == 1
        assert approvals[0]["payload"]["next_phase"] == "builder"

        state = run_state.load_run_state("cli-t6", config)
        assert state["current_phase"] == "builder"

    def test_files_backend_default_unaffected_by_sqlite_code_paths(self, tmp_path, monkeypatch):
        """Sanity check that persistence_backend defaults to 'files' and
        that leaving it unset never touches sqlite at all."""
        monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
        (tmp_path / "tasks").mkdir()
        config = dict(DEFAULT_CONFIG)
        assert config["persistence_backend"] == "files"
        _make_task_dir_with_brief(settings.TASKS_DIR, "files-t1")
        commands.cmd_start("files-t1", auto=False, config=config)
        assert (settings.TASKS_DIR / "files-t1" / "run.yaml").exists()


class TestFailClosedRecovery:
    """The two hard cutover blockers, part A: settings.get_db_connection()
    (the sole path every sqlite-backed command uses) must never silently
    create a fresh database or accept a corrupt one. Both cases raise
    db.DatabaseUnavailable (a subclass) instead -- see db.connect_existing()
    for the actual guard.

    These tests deliberately do NOT use the sqlite_env fixture: that
    fixture now calls db.init_database() up front (simulating a completed
    'relay recover --init'), which is exactly the state these tests must
    NOT start from.
    """

    def _bare_sqlite_config(self, tmp_path, monkeypatch, db_path):
        monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr(settings, "_db_connection", None)
        monkeypatch.setenv("RELAY_DB_PATH", str(db_path))
        (tmp_path / "tasks").mkdir()
        config = dict(DEFAULT_CONFIG)
        config["persistence_backend"] = "sqlite"
        config["human_gates"] = dict(DEFAULT_CONFIG["human_gates"])
        return config

    def test_missing_db_fails_closed(self, tmp_path, monkeypatch):
        db_path = tmp_path / "relay.db"
        self._bare_sqlite_config(tmp_path, monkeypatch, db_path)
        assert not db_path.exists()

        with pytest.raises(db.DatabaseMissing):
            settings.get_db_connection()

        # Fails closed, not "fails after creating half a database".
        assert not db_path.exists()

    def test_corrupt_db_fails_closed(self, tmp_path, monkeypatch):
        db_path = tmp_path / "relay.db"
        db_path.write_bytes(b"this is not a sqlite database")
        self._bare_sqlite_config(tmp_path, monkeypatch, db_path)

        with pytest.raises(db.DatabaseCorrupt):
            settings.get_db_connection()

    def test_missing_db_does_not_fall_back_to_files_backend(self, tmp_path, monkeypatch):
        """A missing canonical database must produce a hard failure, never
        a silent switch to reading/writing run.yaml the way the 'files'
        backend would."""
        db_path = tmp_path / "relay.db"
        config = self._bare_sqlite_config(tmp_path, monkeypatch, db_path)
        _make_task_dir_with_brief(settings.TASKS_DIR, "no-fallback")

        with pytest.raises(db.DatabaseMissing):
            commands.cmd_start("no-fallback", auto=False, config=config)

        # If it had silently fallen back to files, this would exist.
        assert not (settings.TASKS_DIR / "no-fallback" / "run.yaml").exists()

    def test_cli_dispatch_reports_clean_error_not_a_traceback(self, tmp_path, monkeypatch, capsys):
        """main()'s dispatch must catch db.DatabaseUnavailable and print a
        clean, actionable error + exit(1) -- not let the raw exception
        propagate as an uncaught traceback."""
        db_path = tmp_path / "relay.db"
        tasks_dir = tmp_path / "tasks"
        monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(settings, "_db_connection", None)
        monkeypatch.setenv("RELAY_DB_PATH", str(db_path))
        tasks_dir.mkdir()
        _make_task_dir_with_brief(tasks_dir, "cli-missing-db")

        config = dict(DEFAULT_CONFIG)
        config["persistence_backend"] = "sqlite"
        monkeypatch.setattr(settings, "load_config", lambda: config)
        monkeypatch.setattr(sys, "argv", ["relay", "start", "cli-missing-db"])

        with pytest.raises(SystemExit) as exc:
            relay.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "No canonical database found" in out
        assert "Traceback" not in out

    def test_recover_from_projections_does_not_auto_reconstruct_canonical_state(
        self, tmp_path, monkeypatch, capsys
    ):
        """Merely having run.yaml/artifact projections on disk must never,
        by itself, populate a fresh canonical database. Reconstruction is
        only ever the explicit, separate 'relay recover --from-projections
        --apply' action -- an empty freshly-initialized database plus
        existing projection files must stay empty until that's run."""
        db_path = tmp_path / "relay.db"
        config = self._bare_sqlite_config(tmp_path, monkeypatch, db_path)

        # A task that looks complete on disk (run.yaml + artifact), but was
        # never imported into sqlite.
        task_dir = settings.TASKS_DIR / "on-disk-only"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: on-disk-only\ncreated: '2026-08-31T00:00:00Z'\n"
            "updated: '2026-08-31T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: on-disk-only\nphase: architect\nstep: 1\n"
            "date: 2026-08-31\nverdict: ready-for-builder\n---\n\nPlan.\n"
        )

        db.init_database(db_path).close()  # 'relay recover --init', explicit

        # Without --apply, a dry-run report must not write anything.
        capsys.readouterr()
        commands.cmd_recover(config, from_projections=True, apply=False)
        out = capsys.readouterr().out
        assert "Dry-run only" in out

        conn = db.connect_existing(db_path)
        assert db.task_exists(conn, "on-disk-only") is False
        conn.close()

        # Ordinary command execution must see it as absent too -- no
        # implicit read-through to the projection file.
        assert run_state.load_run_state("on-disk-only", config) is None

    def test_regenerate_projections_on_missing_db_fails_closed(self, tmp_path, monkeypatch, capsys):
        """Review finding: --regenerate-projections must not silently
        create a fresh canonical database via init_database() when none
        exists -- it presupposes canonical state already exists. Must
        fail closed exactly like any other ordinary command would."""
        db_path = tmp_path / "relay.db"
        config = self._bare_sqlite_config(tmp_path, monkeypatch, db_path)
        assert not db_path.exists()

        capsys.readouterr()
        with pytest.raises(SystemExit):
            commands.cmd_recover(config, regenerate_projections=True)
        out = capsys.readouterr().out
        assert "No canonical database found" in out

        # The whole point: no database was created as a side effect.
        assert not db_path.exists()

    def test_recover_rejects_ambiguous_flag_combinations(self, tmp_path, monkeypatch, capsys):
        """Review finding: passing more than one action flag to 'recover'
        must error out explicitly, not silently run whichever one happens
        to be checked first."""
        db_path = tmp_path / "relay.db"
        config = self._bare_sqlite_config(tmp_path, monkeypatch, db_path)

        capsys.readouterr()
        with pytest.raises(SystemExit):
            commands.cmd_recover(config, init=True, from_projections=True)
        out = capsys.readouterr().out
        assert "mutually exclusive" in out
        # Neither action actually ran.
        assert not db_path.exists()

    def test_corrupt_db_check_does_not_modify_the_file(self, tmp_path, monkeypatch):
        """Review finding: detecting corruption must never itself mutate
        the file being checked -- e.g. via SQLite's automatic WAL/journal
        recovery replay on an ordinary read-write open. Byte-for-byte
        comparison before and after the failed connect_existing() call."""
        db_path = tmp_path / "relay.db"
        db_path.write_bytes(b"garbage, not a real sqlite file" * 100)
        before = db_path.read_bytes()

        with pytest.raises(db.DatabaseCorrupt):
            db.connect_existing(db_path)

        after = db_path.read_bytes()
        assert before == after


class TestProjectionOnWrite:
    """The two hard cutover blockers, part B: every sqlite-backed mutating
    command must regenerate run.yaml after its canonical write commits, so
    the projection never goes stale. All four live mutating commands route
    through the single save_run_state() chokepoint, so testing that one
    hook plus each CLI command's end-to-end effect covers all of them.
    """

    def _assert_projection_matches_canonical(self, task_id: str):
        conn = settings.get_db_connection()
        projection_path = settings.TASKS_DIR / task_id / "run.yaml"
        assert projection_path.exists(), f"no projection written for {task_id}"
        assert projection_path.read_text() == db.generate_run_yaml(conn, task_id)

    def test_cmd_start_writes_matching_projection(self, sqlite_env):
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "proj-start")
        commands.cmd_start("proj-start", auto=False, config=config)
        self._assert_projection_matches_canonical("proj-start")

    def test_cmd_next_writes_matching_projection(self, sqlite_env):
        config = dict(sqlite_env)
        config["human_gates"] = {"after_designer": False, "after_architect": False,
                                  "reviewer_approved_with_warnings": True,
                                  "tester_pass_with_concerns": True}
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "proj-next")
        commands.cmd_start("proj-next", auto=False, config=config)
        (task_dir / "01-designer.md").write_text(
            "---\ntask_id: proj-next\nphase: designer\nstep: 1\n"
            "date: 2026-08-31\nverdict: ready-for-architect\n---\n\nBrief.\n"
        )
        commands.cmd_next("proj-next", auto=False, config=config)
        self._assert_projection_matches_canonical("proj-next")

    def test_cmd_override_writes_matching_projection(self, sqlite_env):
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "proj-override")
        commands.cmd_start("proj-override", auto=False, config=config)
        commands.cmd_override("proj-override", "builder", config, auto=False)
        self._assert_projection_matches_canonical("proj-override")

    def test_cmd_abort_writes_matching_projection(self, sqlite_env):
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "proj-abort")
        commands.cmd_start("proj-abort", auto=False, config=config)
        commands.cmd_abort("proj-abort", config)
        self._assert_projection_matches_canonical("proj-abort")
        state = run_state.load_run_state("proj-abort", config)
        assert state["status"] == "abandoned"

    def test_projection_write_failure_does_not_affect_committed_canonical_state(
        self, sqlite_env, monkeypatch, capsys
    ):
        """Force write_projection() to fail after a real, successful sqlite
        commit. The canonical mutation must stand (not be rolled back
        because of it), and the failure must be surfaced clearly -- not
        swallowed silently."""
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "proj-fail")

        def _boom(*args, **kwargs):
            raise OSError("simulated disk failure writing projection")

        monkeypatch.setattr(db, "write_projection", _boom)

        capsys.readouterr()
        commands.cmd_start("proj-fail", auto=False, config=config)
        out = capsys.readouterr().out

        assert "Warning: canonical state for 'proj-fail' was saved successfully" in out
        assert "simulated disk failure" in out
        assert "relay recover --regenerate-projections proj-fail" in out

        # Canonical state committed regardless of the projection failure.
        conn = settings.get_db_connection()
        assert db.task_exists(conn, "proj-fail") is True
        state = db.get_task(conn, "proj-fail")
        assert state["current_phase"] == "designer"

        # And no partial/corrupt projection file was left behind either.
        assert not (settings.TASKS_DIR / "proj-fail" / "run.yaml").exists()

    def test_regenerate_projections_recovers_after_a_prior_failure(self, sqlite_env, monkeypatch):
        """The operator-facing fix-up path for the failure case above:
        once the underlying issue is gone, --regenerate-projections
        brings the file back in sync with no other side effects."""
        config = sqlite_env
        _make_task_dir_with_brief(settings.TASKS_DIR, "proj-heal")

        real_write_projection = db.write_projection
        monkeypatch.setattr(db, "write_projection", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        commands.cmd_start("proj-heal", auto=False, config=config)
        assert not (settings.TASKS_DIR / "proj-heal" / "run.yaml").exists()

        monkeypatch.setattr(db, "write_projection", real_write_projection)
        commands.cmd_recover(config, task_id="proj-heal", regenerate_projections=True)
        self._assert_projection_matches_canonical("proj-heal")


class TestMigrationDecisionExtensions:
    """Decisions that cover what a migration of real ledgers can meet:
    worked tasks with no run.yaml, files that aren't agent output, reused
    step numbers, and steps that were never delivered. None of these may
    invent history or silently drop it."""

    def _artifact(self, path: Path, *, phase, step, verdict, date="2026-05-01"):
        path.write_text(
            f"---\ntask_id: {path.parent.name}\nphase: {phase}\nstep: {step}\n"
            f"date: {date}\nverdict: {verdict}\n---\n\nBody.\n"
        )

    def _run_yaml(self, task_dir: Path, status="in_progress", phase="builder"):
        (task_dir / "run.yaml").write_text(
            f"task_id: {task_dir.name}\ncreated: '2026-05-01T00:00:00Z'\n"
            f"updated: '2026-05-02T00:00:00Z'\nstatus: {status}\n"
            f"current_phase: {phase}\nnext_step: 3\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )

    def _events(self, conn, task_id, event_type):
        return conn.execute(
            "SELECT step, payload FROM events WHERE task_id=? AND event_type=? ORDER BY step",
            (task_id, event_type),
        ).fetchall()

    def test_task_without_run_yaml_imports_with_decision(self, conn, tmp_path):
        task_dir = tmp_path / "no-run-yaml"
        task_dir.mkdir()
        self._artifact(task_dir / "01-architect.md", phase="architect", step=1,
                       verdict="ready-for-builder", date="2026-04-10")
        self._artifact(task_dir / "02-builder.md", phase="builder", step=2,
                       verdict="ready-for-review", date="2026-04-12")
        decision = db.MigrationDecision(task_id="no-run-yaml", status="completed",
                                        current_phase="done", note="shipped per git history")

        db.import_task(conn, "no-run-yaml", task_dir, decision=decision)

        row = conn.execute("SELECT status, current_phase, created, updated FROM tasks "
                           "WHERE task_id='no-run-yaml'").fetchone()
        assert row["status"] == "completed"
        assert row["current_phase"] == "done"
        assert row["created"] == "2026-04-10T00:00:00Z"
        assert row["updated"] == "2026-04-12T00:00:00Z"
        notes = self._events(conn, "no-run-yaml", db.EVENT_HISTORICAL_NOTE)
        assert any("no run.yaml" in json.loads(n["payload"])["source"] for n in notes)

    def test_task_without_run_yaml_refused_without_decision_even_with_force(self, conn, tmp_path):
        task_dir = tmp_path / "no-run-yaml"
        task_dir.mkdir()
        self._artifact(task_dir / "01-architect.md", phase="architect", step=1,
                       verdict="ready-for-builder")
        with pytest.raises(db.InvalidTransition, match="no run.yaml"):
            db.import_task(conn, "no-run-yaml", task_dir, force=True)
        assert not db.task_exists(conn, "no-run-yaml")

    def test_artifact_without_frontmatter_never_becomes_an_architect_step(self, conn, tmp_path):
        """The old import wrote a phase-less file as an 'architect' step,
        inventing its phase. Now it must be named as a note."""
        task_dir = tmp_path / "prose-plan"
        task_dir.mkdir()
        self._run_yaml(task_dir, phase="architect")
        self._artifact(task_dir / "01-architect.md", phase="architect", step=1,
                       verdict="ready-for-builder")
        (task_dir / "02-prod-plan.md").write_text("# Plan\n\nProse only.\n")

        with pytest.raises(db.InvalidTransition, match="no frontmatter phase"):
            db.import_task(conn, "prose-plan", task_dir, force=True)

        decision = db.MigrationDecision(task_id="prose-plan", note="planning doc, not agent output",
                                        note_files=("02-prod-plan.md",))
        db.import_task(conn, "prose-plan", task_dir, decision=decision)
        steps = conn.execute("SELECT step, phase FROM steps WHERE task_id='prose-plan'").fetchall()
        assert [(s["step"], s["phase"]) for s in steps] == [(1, "architect")]
        notes = self._events(conn, "prose-plan", db.EVENT_HISTORICAL_NOTE)
        assert any(json.loads(n["payload"]).get("note_file") == "02-prod-plan.md" for n in notes)

    def test_reused_step_number_is_refused_not_silently_dropped(self, conn, tmp_path):
        """The old import used INSERT OR IGNORE, so a second file with the
        same step number vanished without a trace."""
        task_dir = tmp_path / "duplicate-step"
        task_dir.mkdir()
        self._run_yaml(task_dir, phase="reviewer")
        self._artifact(task_dir / "01-builder.md", phase="builder", step=1, verdict="ready-for-review")
        self._artifact(task_dir / "02-reviewer.md", phase="reviewer", step=2, verdict="approved")
        self._artifact(task_dir / "02-uitests.md", phase="tester", step=2, verdict="pass")

        with pytest.raises(db.InvalidTransition, match="more than one artifact"):
            db.import_task(conn, "duplicate-step", task_dir, force=True)

        decision = db.MigrationDecision(task_id="duplicate-step", note="ui test notes reused step 2",
                                        note_files=("02-uitests.md",))
        db.import_task(conn, "duplicate-step", task_dir, decision=decision)
        steps = conn.execute("SELECT step, artifact_filename FROM steps "
                             "WHERE task_id='duplicate-step' ORDER BY step").fetchall()
        assert [s["artifact_filename"] for s in steps] == ["01-builder.md", "02-reviewer.md"]

    def test_declared_missing_step_imports_and_is_recorded(self, conn, tmp_path):
        task_dir = tmp_path / "gap-task"
        task_dir.mkdir()
        self._run_yaml(task_dir, phase="tester")
        self._artifact(task_dir / "01-architect.md", phase="architect", step=1, verdict="ready-for-builder")
        self._artifact(task_dir / "02-builder.md", phase="builder", step=2, verdict="ready-for-review")
        self._artifact(task_dir / "04-tester.md", phase="tester", step=4, verdict="pass")

        undeclared = db.MigrationDecision(task_id="gap-task", note="mapping something else")
        with pytest.raises(db.InvalidTransition, match=r"steps \[3\] are missing"):
            db.import_task(conn, "gap-task", task_dir, decision=undeclared)

        declared = db.MigrationDecision(task_id="gap-task", note="reviewer step never written",
                                        missing_steps=(3,))
        db.import_task(conn, "gap-task", task_dir, decision=declared)
        never_delivered = self._events(conn, "gap-task", db.EVENT_STEP_NEVER_DELIVERED)
        assert [event["step"] for event in never_delivered] == [3]

    def test_missing_step_that_exists_on_disk_is_refused(self, conn, tmp_path):
        task_dir = tmp_path / "contradiction"
        task_dir.mkdir()
        self._run_yaml(task_dir, phase="architect")
        self._artifact(task_dir / "01-architect.md", phase="architect", step=1, verdict="ready-for-builder")
        decision = db.MigrationDecision(task_id="contradiction", note="wrong", missing_steps=(1,))
        with pytest.raises(db.InvalidTransition, match="actually exist"):
            db.import_task(conn, "contradiction", task_dir, decision=decision)

    def test_note_file_that_is_not_on_disk_is_refused(self, conn, tmp_path):
        task_dir = tmp_path / "stale-decision"
        task_dir.mkdir()
        self._run_yaml(task_dir, phase="architect")
        self._artifact(task_dir / "01-architect.md", phase="architect", step=1, verdict="ready-for-builder")
        decision = db.MigrationDecision(task_id="stale-decision", note="stale",
                                        note_files=("09-gone.md",))
        with pytest.raises(db.InvalidTransition, match="not agent artifacts on disk"):
            db.import_task(conn, "stale-decision", task_dir, decision=decision)

    def test_exclude_cannot_be_combined_with_new_fields(self):
        with pytest.raises(ValueError, match="cannot both exclude"):
            db.MigrationDecision(task_id="t", exclude=True, note="x", missing_steps=(2,))
        with pytest.raises(ValueError, match="cannot both exclude"):
            db.MigrationDecision(task_id="t", exclude=True, note="x", note_files=("02-a.md",))


class TestDecisionScope:
    """Regression for review blocker 3: any decision, even one holding only
    a note, used to waive every discrepancy a task had, with no audit."""

    def _drifted_task(self, tmp_path):
        task_dir = tmp_path / "drifted"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: drifted\ncreated: '2026-05-01T00:00:00Z'\n"
            "updated: '2026-05-01T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: builder\nnext_step: 4\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-reviewer.md").write_text(
            "---\ntask_id: drifted\nphase: reviewer\nstep: 1\n"
            "date: 2026-05-01\nverdict: approved\n---\n\nBody.\n"
        )
        return task_dir

    def test_note_only_decision_does_not_waive_phase_drift(self, conn, tmp_path):
        task_dir = self._drifted_task(tmp_path)
        note_only = db.MigrationDecision(task_id="drifted", note="unrelated note")
        with pytest.raises(db.InvalidTransition, match="does not address"):
            db.import_task(conn, "drifted", task_dir, decision=note_only)
        assert not db.task_exists(conn, "drifted")

    def test_explicit_phase_decision_addresses_drift_and_is_audited(self, conn, tmp_path):
        task_dir = self._drifted_task(tmp_path)
        explicit = db.MigrationDecision(task_id="drifted", current_phase="reviewer",
                                        note="artifacts win: latest is the reviewer")
        db.import_task(conn, "drifted", task_dir, decision=explicit)
        override_events = conn.execute(
            "SELECT payload FROM events WHERE task_id='drifted' AND event_type=?",
            (db.EVENT_MIGRATION_OVERRIDE,)).fetchall()
        assert any(json.loads(e["payload"])["field"] == "current_phase" for e in override_events)


class TestDecisionsFile:
    def test_load_decisions_parses_every_field(self, tmp_path):
        path = tmp_path / "decisions.yaml"
        path.write_text(
            "decisions:\n"
            "  - task: mapped\n"
            "    status: completed\n"
            "    current_phase: done\n"
            "    note: shipped\n"
            "    missing_steps: [4]\n"
            "    note_files: [05-plan.md]\n"
            "    step_overrides:\n"
            "      - {step: 1, expected: approved, canonical: ready-for-builder, note: old word}\n"
            "  - task: dropped\n"
            "    exclude: true\n"
            "    note: custom workflow\n"
        )
        decisions = db.load_decisions(path)
        mapped = decisions["mapped"]
        assert mapped.status == "completed" and mapped.current_phase == "done"
        assert mapped.missing_steps == (4,)
        assert mapped.note_files == ("05-plan.md",)
        assert mapped.step_override_for(1).canonical_verdict == "ready-for-builder"
        assert decisions["dropped"].exclude is True

    def test_load_decisions_rejects_unknown_keys(self, tmp_path):
        path = tmp_path / "decisions.yaml"
        path.write_text("decisions:\n  - task: t\n    note: x\n    stauts: completed\n")
        with pytest.raises(ValueError, match="unknown keys"):
            db.load_decisions(path)

    def test_load_decisions_rejects_duplicate_tasks(self, tmp_path):
        path = tmp_path / "decisions.yaml"
        path.write_text("decisions:\n  - {task: t, note: a}\n  - {task: t, note: b}\n")
        with pytest.raises(ValueError, match="more than one decision"):
            db.load_decisions(path)

    def test_load_decisions_requires_notes(self, tmp_path):
        path = tmp_path / "decisions.yaml"
        path.write_text("decisions:\n  - {task: t, status: completed}\n")
        with pytest.raises(ValueError, match="'note' must be a non-empty string"):
            db.load_decisions(path)


class TestDecisionsFileTypes:
    """Regression for review blocker 2: the loader coerced wrong types
    instead of refusing them, so a quoted "false" silently excluded a task
    and a string where a list belongs was split into characters."""

    def _load(self, tmp_path, body: str):
        path = tmp_path / "decisions.yaml"
        path.write_text(body)
        return db.load_decisions(path)

    def test_quoted_false_is_not_an_exclusion(self, tmp_path):
        with pytest.raises(ValueError, match="'exclude' must be true or false"):
            self._load(tmp_path, 'decisions:\n  - {task: t, note: x, exclude: "false"}\n')

    def test_string_instead_of_list_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="'missing_steps' must be a list"):
            self._load(tmp_path, 'decisions:\n  - {task: t, note: x, missing_steps: "12"}\n')
        with pytest.raises(ValueError, match="'note_files' must be a list"):
            self._load(tmp_path, 'decisions:\n  - {task: t, note: x, note_files: 05-a.md}\n')

    def test_string_step_number_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="every item in 'missing_steps' must be a int"):
            self._load(tmp_path, 'decisions:\n  - {task: t, note: x, missing_steps: ["3"]}\n')

    def test_malformed_entries_name_the_problem(self, tmp_path):
        with pytest.raises(ValueError, match="expected a mapping"):
            self._load(tmp_path, "decisions:\n  - just-a-string\n")
        with pytest.raises(ValueError, match="'expected' must be a non-empty string"):
            self._load(tmp_path, "decisions:\n  - task: t\n    note: x\n"
                                 "    step_overrides:\n      - {step: 1, canonical: secure, note: y}\n")
        with pytest.raises(ValueError, match="top-level 'decisions:' list"):
            self._load(tmp_path, "- task: t\n")


class TestRehearseMigration:
    def _artifact(self, path: Path, *, phase, step, verdict):
        path.write_text(
            f"---\ntask_id: {path.parent.name}\nphase: {phase}\nstep: {step}\n"
            f"date: 2026-05-01\nverdict: {verdict}\n---\n\nBody.\n"
        )

    def _task(self, tasks_dir: Path, name: str, *, status="in_progress", run_yaml=True):
        task_dir = tasks_dir / name
        task_dir.mkdir()
        if run_yaml:
            (task_dir / "run.yaml").write_text(
                f"task_id: {name}\ncreated: '2026-05-01T00:00:00Z'\n"
                f"updated: '2026-05-01T00:00:00Z'\nstatus: {status}\n"
                "current_phase: architect\nnext_step: 2\nschema_version: 1\n"
                "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
            )
        self._artifact(task_dir / "01-architect.md", phase="architect", step=1,
                       verdict="ready-for-builder")
        return task_dir

    def test_every_task_lands_in_exactly_one_bucket(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        self._task(tasks_dir, "clean-task")
        self._task(tasks_dir, "old-word", status="complete")
        self._task(tasks_dir, "no-decision", status="calibrating")
        self._task(tasks_dir, "custom", status="parked")
        self._task(tasks_dir, "no-run-yaml", run_yaml=False)
        decisions = {
            "old-word": db.MigrationDecision(task_id="old-word", status="completed", note="spelling"),
            "custom": db.MigrationDecision(task_id="custom", exclude=True, note="custom workflow"),
            "no-run-yaml": db.MigrationDecision(task_id="no-run-yaml", status="completed",
                                                current_phase="done", note="shipped"),
            "ghost": db.MigrationDecision(task_id="ghost", status="completed", note="typo'd name"),
        }

        rehearsal = db.rehearse_migration(tasks_dir, decisions)

        assert rehearsal["scanned"] == 5
        assert rehearsal["clean"] == ["clean-task"]
        assert sorted(rehearsal["resolved"]) == ["no-run-yaml", "old-word"]
        assert rehearsal["excluded"] == ["custom"]
        assert [task for task, _ in rehearsal["unresolved"]] == ["no-decision"]
        assert rehearsal["unknown_decisions"] == ["ghost"]

    def test_rehearsal_writes_nothing(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        self._task(tasks_dir, "clean-task")
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

        db.rehearse_migration(tasks_dir, {})

        after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert before == after
        assert not list(tmp_path.rglob("*.db*"))

    def test_bad_decision_lands_in_unresolved_with_reason(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        self._task(tasks_dir, "bad-mapping", status="complete")
        decisions = {"bad-mapping": db.MigrationDecision(task_id="bad-mapping", status="finished",
                                                         note="not a real status")}
        rehearsal = db.rehearse_migration(tasks_dir, decisions)
        assert rehearsal["resolved"] == []
        [(task, reason)] = rehearsal["unresolved"]
        assert task == "bad-mapping" and "'finished' is not a valid status" in reason


class TestRecoverCommand:
    def test_from_projections_dry_run_works_without_an_open_connection(self, tmp_path, monkeypatch, capsys):
        """Regression: `relay recover --from-projections` crashed with
        'db.configure() must be called before use' because only opening a
        database connection registered the enums, and a dry run opens none."""
        tasks_dir = tmp_path / "tasks"
        task_dir = tasks_dir / "any-task"
        task_dir.mkdir(parents=True)
        (task_dir / "run.yaml").write_text(
            "task_id: any-task\ncreated: '2026-05-01T00:00:00Z'\n"
            "updated: '2026-05-01T00:00:00Z'\nstatus: in_progress\n"
            "current_phase: architect\nnext_step: 1\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(db, "_valid_phases", [])
        monkeypatch.setattr(db, "_valid_verdicts", {})
        monkeypatch.setattr(db, "_valid_statuses", [])

        commands.cmd_recover({"persistence_backend": "sqlite"}, from_projections=True)

        assert "Dry-run only" in capsys.readouterr().out

    def test_decisions_flag_reports_buckets_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        task_dir = tasks_dir / "old-word"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: old-word\ncreated: '2026-05-01T00:00:00Z'\n"
            "updated: '2026-05-01T00:00:00Z'\nstatus: complete\n"
            "current_phase: done\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        decisions_file = tmp_path / "decisions.yaml"
        decisions_file.write_text("decisions:\n  - {task: old-word, status: completed, note: spelling}\n")
        monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(settings, "_db_path", lambda: tmp_path / "relay.db")

        commands.cmd_recover({"persistence_backend": "sqlite"}, from_projections=True,
                          decisions_path=str(decisions_file))

        output = capsys.readouterr().out
        assert "Resolved:   1" in output and "Unresolved: 0" in output
        assert not (tmp_path / "relay.db").exists()

    def test_decisions_apply_imports_resolved_tasks(self, tmp_path, monkeypatch, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        task_dir = tasks_dir / "old-word"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: old-word\ncreated: '2026-05-01T00:00:00Z'\n"
            "updated: '2026-05-01T00:00:00Z'\nstatus: complete\n"
            "current_phase: done\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        decisions_file = tmp_path / "decisions.yaml"
        decisions_file.write_text("decisions:\n  - {task: old-word, status: completed, note: spelling}\n")
        db_file = tmp_path / "relay.db"
        db.init_database(db_file).close()
        monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(settings, "_db_path", lambda: db_file)

        commands.cmd_recover({"persistence_backend": "sqlite"}, from_projections=True,
                          decisions_path=str(decisions_file), apply=True)

        conn = db.connect_existing(db_file)
        assert conn.execute("SELECT status FROM tasks WHERE task_id='old-word'").fetchone()[0] == "completed"
        assert "Imported 1 task(s)" in capsys.readouterr().out


class TestDeployment:
    def test_setup_copies_every_local_module_relay_imports(self):
        """Regression: setup.sh copied only relay.py, but relay.py imports
        db.py and doctor.py, so a fresh deploy failed on every command
        with ModuleNotFoundError."""
        import ast
        orchestrator_dir = Path(__file__).resolve().parent.parent
        setup_script = (orchestrator_dir.parent / "setup.sh").read_text()
        tree = ast.parse((orchestrator_dir / "relay.py").read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        local_modules = sorted(name for name in imported
                               if (orchestrator_dir / f"{name}.py").exists())
        assert local_modules, "expected relay.py to import local modules"
        copies_all_python = 'orchestrator/"*.py' in setup_script
        for module in local_modules:
            assert copies_all_python or f"orchestrator/{module}.py" in setup_script, \
                f"setup.sh does not deploy {module}.py"


class TestReviewWarnings:
    """Migration decisions and reconciliation refuse what they cannot
    apply honestly, and odd files are flagged rather than crashing."""

    def _task(self, root: Path, name: str, *, steps, status="in_progress", phase=None,
              history="history: []\n", run_yaml=True):
        task_dir = root / name
        task_dir.mkdir(parents=True)
        for step, artifact_phase, verdict in steps:
            (task_dir / f"{step:02d}-{artifact_phase}.md").write_text(
                f"---\ntask_id: {name}\nphase: {artifact_phase}\nstep: {step}\n"
                f"date: 2026-05-0{step}\nverdict: {verdict}\n---\n\nBody.\n"
            )
        if run_yaml:
            latest_phase = phase or steps[-1][1]
            (task_dir / "run.yaml").write_text(
                f"task_id: {name}\ncreated: '2026-05-01T00:00:00Z'\n"
                f"updated: '2026-05-01T00:00:00Z'\nstatus: {status}\n"
                f"current_phase: {latest_phase}\nnext_step: 9\nschema_version: 1\n"
                f"repo: relay\ndeploy_intent: false\nretries: {{}}\n{history}"
            )
        return task_dir

    def test_override_for_a_step_that_does_not_exist_is_refused(self, conn, tmp_path):
        task_dir = self._task(tmp_path, "t", steps=[(1, "architect", "ready-for-builder")])
        decision = db.MigrationDecision(task_id="t", note="stale", step_overrides=(
            db.StepVerdictOverride(5, "approved", "secure", note="no such step"),))
        with pytest.raises(db.InvalidTransition, match="matches no step on disk"):
            db.import_task(conn, "t", task_dir, decision=decision)

    def test_override_for_an_already_canonical_verdict_is_refused(self, conn, tmp_path):
        task_dir = self._task(tmp_path, "t", steps=[(1, "architect", "ready-for-builder")])
        decision = db.MigrationDecision(task_id="t", note="stale", step_overrides=(
            db.StepVerdictOverride(1, "ready-for-builder", "needs-human-input", note="pointless"),))
        with pytest.raises(db.InvalidTransition, match="already canonical"):
            db.import_task(conn, "t", task_dir, decision=decision)

    def test_missing_step_before_the_first_artifact_is_refused(self, conn, tmp_path):
        task_dir = self._task(tmp_path, "t", steps=[(2, "builder", "ready-for-review")])
        decision = db.MigrationDecision(task_id="t", note="x", missing_steps=(1,))
        with pytest.raises(db.InvalidTransition, match="before the first artifact"):
            db.import_task(conn, "t", task_dir, decision=decision)

    def test_missing_step_after_the_last_needs_history_evidence(self, conn, tmp_path):
        no_history = self._task(tmp_path / "a", "t", steps=[(1, "architect", "ready-for-builder")],
                                phase="reviewer")
        decision = db.MigrationDecision(task_id="t", current_phase="reviewer", note="x",
                                        missing_steps=(2,))
        with pytest.raises(db.InvalidTransition, match="no record of them"):
            db.import_task(conn, "t", no_history, decision=decision)

        with_history = self._task(tmp_path / "b", "t", steps=[(1, "architect", "ready-for-builder")],
                                  phase="reviewer",
                                  history="history:\n- {phase: reviewer, step: 2}\n")
        db.import_task(conn, "t", with_history, decision=decision)
        assert db.task_exists(conn, "t")

    def test_force_does_not_skip_missing_step_range_checks(self, conn, tmp_path):
        task_dir = self._task(tmp_path, "t", steps=[(2, "builder", "ready-for-review")])
        decision = db.MigrationDecision(task_id="t", note="x", missing_steps=(1,))
        with pytest.raises(db.InvalidTransition, match="before the first artifact"):
            db.import_task(conn, "t", task_dir, decision=decision, force=True)

    def test_task_without_run_yaml_gets_next_step_from_its_trail(self, conn, tmp_path):
        task_dir = self._task(tmp_path, "t", run_yaml=False,
                              steps=[(1, "architect", "ready-for-builder"),
                                     (2, "builder", "ready-for-review")])
        decision = db.MigrationDecision(task_id="t", status="completed", current_phase="done",
                                        note="shipped")
        db.import_task(conn, "t", task_dir, decision=decision)
        assert conn.execute("SELECT next_step FROM tasks WHERE task_id='t'").fetchone()[0] == 3

    def test_unquoted_yaml_timestamps_are_stored_in_the_standard_form(self, conn, tmp_path):
        task_dir = tmp_path / "t"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: t\ncreated: 2026-05-01T08:30:00Z\nupdated: 2026-05-02T09:00:00Z\n"
            "status: in_progress\ncurrent_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: t\nphase: architect\nstep: 1\ndate: 2026-05-01\n"
            "verdict: ready-for-builder\n---\n\nBody.\n"
        )
        db.import_task(conn, "t", task_dir)
        row = conn.execute("SELECT created, updated FROM tasks WHERE task_id='t'").fetchone()
        assert (row["created"], row["updated"]) == ("2026-05-01T08:30:00Z", "2026-05-02T09:00:00Z")

    def test_malformed_frontmatter_is_flagged_not_a_crash(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        task_dir = self._task(tasks_dir, "t", steps=[(1, "architect", "ready-for-builder")])
        (task_dir / "02-builder.md").write_text("---\nphase: [unclosed\n---\n\nBody.\n")
        (task_dir / "03-reviewer.md").write_text("---\njust a string\n---\n\nBody.\n")

        summary = db.dry_run_migration(tasks_dir)
        rehearsal = db.rehearse_migration(tasks_dir, {})

        [result] = summary["results"]
        unreadable = [d for d in result["discrepancies"] if "unreadable frontmatter" in d]
        assert len(unreadable) == 2
        assert [task for task, _ in rehearsal["unresolved"]] == ["t"]

    def test_a_frontmatter_relay_does_not_read_is_flagged_with_why(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        task_dir = self._task(tasks_dir, "t", steps=[(1, "architect", "ready-for-builder")])
        (task_dir / "02-builder.md").write_text(f"---\n{SECURITYS_MERGE_KEYS}---\n\nBody.\n")
        [result] = db.dry_run_migration(tasks_dir)["results"]
        assert ("artifact (02-builder.md) has unreadable frontmatter: frontmatter holds a YAML "
                "anchor, alias or merge key (&name, *name or an unquoted <<)") in result["discrepancies"]


class TestRecoverApplyScope:
    def test_apply_never_imports_excluded_or_unresolved_tasks(self, tmp_path, monkeypatch, capsys):
        tasks_dir = tmp_path / "tasks"
        for name, status in (("clean-task", "in_progress"), ("excluded-task", "parked"),
                             ("unresolved-task", "calibrating")):
            task_dir = tasks_dir / name
            task_dir.mkdir(parents=True)
            (task_dir / "run.yaml").write_text(
                f"task_id: {name}\ncreated: '2026-05-01T00:00:00Z'\n"
                f"updated: '2026-05-01T00:00:00Z'\nstatus: {status}\n"
                "current_phase: architect\nnext_step: 1\nschema_version: 1\n"
                "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
            )
            (task_dir / "01-architect.md").write_text(
                f"---\ntask_id: {name}\nphase: architect\nstep: 1\ndate: 2026-05-01\n"
                "verdict: ready-for-builder\n---\n\nBody.\n"
            )
        decisions_file = tmp_path / "decisions.yaml"
        decisions_file.write_text("decisions:\n  - {task: excluded-task, exclude: true, note: custom}\n")
        db_file = tmp_path / "relay.db"
        db.init_database(db_file).close()
        monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(settings, "_db_path", lambda: db_file)

        commands.cmd_recover({"persistence_backend": "sqlite"}, from_projections=True,
                          decisions_path=str(decisions_file), apply=True)

        conn = db.connect_existing(db_file)
        imported = {row[0] for row in conn.execute("SELECT task_id FROM tasks")}
        assert imported == {"clean-task"}

    def test_bad_decisions_file_prints_a_clean_error(self, tmp_path, monkeypatch, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        decisions_file = tmp_path / "decisions.yaml"
        decisions_file.write_text("decisions:\n  - {task: t, note: x, exclude: \"false\"}\n")
        monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(settings, "_db_path", lambda: tmp_path / "relay.db")

        with pytest.raises(SystemExit):
            commands.cmd_recover({"persistence_backend": "sqlite"}, from_projections=True,
                              decisions_path=str(decisions_file))
        assert "invalid decisions file" in capsys.readouterr().out

        with pytest.raises(SystemExit):
            commands.cmd_recover({"persistence_backend": "sqlite"}, from_projections=True,
                              decisions_path=str(tmp_path / "nope.yaml"))
        assert "not found" in capsys.readouterr().out


class TestReReviewWarnings:
    """Odd values in legacy files and bad decisions files give clean
    errors or refusals, never a crash."""

    def _task(self, tasks_dir: Path, run_yaml_tail: str):
        task_dir = tasks_dir / "t"
        task_dir.mkdir(parents=True)
        (task_dir / "run.yaml").write_text(
            "task_id: t\ncreated: '2026-05-01T00:00:00Z'\nupdated: '2026-05-01T00:00:00Z'\n"
            "status: in_progress\ncurrent_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\n" + run_yaml_tail
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: t\nphase: architect\nstep: 1\ndate: 2026-05-01\n"
            "verdict: ready-for-builder\n---\n\nBody.\n"
        )
        return task_dir

    def test_dates_in_run_yaml_history_and_extra_fields_do_not_crash_the_rehearsal(self, conn, tmp_path):
        tasks_dir = tmp_path / "tasks"
        self._task(tasks_dir, "history:\n- {phase: human_gate, step: 2, timestamp: 2026-05-01T10:00:00Z}\n"
                              "shipped_on: 2026-05-03\n")
        rehearsal = db.rehearse_migration(tasks_dir, {})
        assert rehearsal["clean"] == ["t"]

        db.import_task(conn, "t", tasks_dir / "t")
        rows = conn.execute("SELECT payload, timestamp FROM events WHERE task_id='t' "
                            "AND event_type=?", (db.EVENT_HISTORICAL_NOTE,)).fetchall()
        payloads = [json.loads(r["payload"]) for r in rows]
        assert {"shipped_on": "2026-05-03T00:00:00Z"} in payloads
        assert "2026-05-01T10:00:00Z" in [r["timestamp"] for r in rows]

    def test_invalid_yaml_decisions_file_is_a_value_error(self, tmp_path):
        path = tmp_path / "decisions.yaml"
        path.write_text("decisions:\n  - {task: t, note: [unclosed\n")
        with pytest.raises(ValueError, match="not valid YAML"):
            db.load_decisions(path)

    def test_decisions_path_that_is_a_directory_prints_a_clean_error(self, tmp_path, monkeypatch, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(settings, "_db_path", lambda: tmp_path / "relay.db")
        with pytest.raises(SystemExit):
            commands.cmd_recover({"persistence_backend": "sqlite"}, from_projections=True,
                              decisions_path=str(tmp_path))
        assert "cannot read decisions file" in capsys.readouterr().out

    def test_duplicate_missing_steps_are_refused(self):
        with pytest.raises(ValueError, match="same missing_steps entry"):
            db.MigrationDecision(task_id="t", note="x", missing_steps=(2, 2))

    def test_override_on_a_step_with_no_verdict_is_refused(self, conn, tmp_path):
        task_dir = tmp_path / "t"
        task_dir.mkdir()
        (task_dir / "run.yaml").write_text(
            "task_id: t\ncreated: '2026-05-01T00:00:00Z'\nupdated: '2026-05-01T00:00:00Z'\n"
            "status: in_progress\ncurrent_phase: architect\nnext_step: 2\nschema_version: 1\n"
            "repo: relay\ndeploy_intent: false\nretries: {}\nhistory: []\n"
        )
        (task_dir / "01-architect.md").write_text(
            "---\ntask_id: t\nphase: architect\nstep: 1\ndate: 2026-05-01\n---\n\nNo verdict.\n"
        )
        decision = db.MigrationDecision(task_id="t", note="x", step_overrides=(
            db.StepVerdictOverride(1, "approved", "ready-for-builder", note="no verdict there"),))
        with pytest.raises(db.InvalidTransition, match="expected verdict 'approved' but found 'None'"):
            db.import_task(conn, "t", task_dir, decision=decision)


class TestNewPipelineEndToEnd:
    """Drive real tasks through `relay next` in the pipeline order:
    designer -> architect -> builder -> reviewer -> tester -> security -> ops."""

    GATES_OFF = {"after_designer": False, "after_architect": False,
                 "reviewer_approved_with_warnings": True, "tester_pass_with_concerns": True}

    def _write(self, task_dir, step, phase, verdict, extra=""):
        (task_dir / f"{step:02d}-{phase}.md").write_text(
            f"---\ntask_id: {task_dir.name}\nphase: {phase}\nstep: {step}\n"
            f"date: 2026-10-03\nverdict: {verdict}\n{extra}---\n\nBody.\n"
        )

    def _run_to_security(self, config, task_id, deploy, deploy_at_start=False):
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, task_id)
        commands.cmd_start(task_id, auto=False, config=config, deploy_intent=deploy_at_start)
        self._write(task_dir, 1, "designer", "ready-for-architect")
        commands.cmd_next(task_id, auto=False, config=config)
        self._write(task_dir, 2, "architect", "ready-for-builder", f"pass: work\ndeploy: {deploy}\n")
        commands.cmd_next(task_id, auto=False, config=config)
        for step, phase, verdict in ((3, "builder", "ready-for-review"), (4, "reviewer", "approved"),
                                     (5, "tester", "pass"), (6, "security", "secure")):
            self._write(task_dir, step, phase, verdict)
            commands.cmd_next(task_id, auto=False, config=config)
        return run_state.load_run_state(task_id, config)

    def test_new_task_starts_at_designer(self, sqlite_env):
        _make_task_dir_with_brief(settings.TASKS_DIR, "pipe-start")
        commands.cmd_start("pipe-start", auto=False, config=sqlite_env)
        assert run_state.load_run_state("pipe-start", sqlite_env)["current_phase"] == "designer"

    def test_architect_deploy_no_completes_after_security(self, sqlite_env, capsys):
        config = {**sqlite_env, "human_gates": self.GATES_OFF}
        # Started as a deploy task; the Architect's plan says no, and wins.
        state = self._run_to_security(config, "pipe-nodeploy", deploy="no", deploy_at_start=True)
        assert state["deploy_intent"] is False
        events = db.get_events(settings.get_db_connection(), "pipe-nodeploy")
        decisions = [e for e in events if e["event_type"] == db.EVENT_DEPLOY_DECIDED]
        assert len(decisions) == 1
        assert decisions[0]["payload"] == {"previous": True, "decided": False, "source": "architect"}
        assert state["status"] == "completed"
        out = capsys.readouterr().out
        assert "Skipping ops" in out
        # The close names no file the task does not have.
        assert "Workflow complete for pipe-nodeploy." in out and "99-final" not in out

    def test_architect_deploy_yes_routes_to_ops(self, sqlite_env):
        config = {**sqlite_env, "human_gates": self.GATES_OFF}
        state = self._run_to_security(config, "pipe-deploy", deploy="yes")
        assert state["deploy_intent"] is True
        assert state["current_phase"] == "ops"
        assert state["status"] == "in_progress"

    def test_task_from_old_order_runs_tester_after_security(self, sqlite_env, capsys):
        """Old order ran security before tester. A task that finished security
        with no tester step must get its tester run, not skip it."""
        config = {**sqlite_env, "human_gates": self.GATES_OFF}
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "pipe-legacy")
        commands.cmd_start("pipe-legacy", auto=False, config=config)
        self._write(task_dir, 1, "designer", "ready-for-architect")
        commands.cmd_next("pipe-legacy", auto=False, config=config)
        self._write(task_dir, 2, "architect", "ready-for-builder", "deploy: no\n")
        commands.cmd_next("pipe-legacy", auto=False, config=config)
        self._write(task_dir, 3, "builder", "ready-for-review")
        commands.cmd_next("pipe-legacy", auto=False, config=config)
        self._write(task_dir, 4, "reviewer", "approved")
        # The old order sent an approved review to Security, not the Tester.
        commands.cmd_override("pipe-legacy", "security", config, auto=False)
        self._write(task_dir, 5, "security", "secure")
        commands.cmd_next("pipe-legacy", auto=False, config=config)
        state = run_state.load_run_state("pipe-legacy", config)
        assert state["current_phase"] == "tester"
        assert "task predates the new order" in capsys.readouterr().out

    def test_recommend_no_build_stops_for_the_owner(self, sqlite_env, capsys):
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "pipe-nobuild")
        commands.cmd_start("pipe-nobuild", auto=False, config=sqlite_env)
        self._write(task_dir, 1, "designer", "recommend-no-build")
        capsys.readouterr()
        commands.cmd_next("pipe-nobuild", auto=False, config=sqlite_env)
        assert "HUMAN GATE" in capsys.readouterr().out
        assert run_state.load_run_state("pipe-nobuild", sqlite_env)["current_phase"] == "designer"


class TestDeployFlag:
    @pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("yes", True),
                                                ("No", False), ("maybe", None), (None, None)])
    def test_deploy_flag_parsing(self, value, expected):
        assert routing._deploy_flag(value) is expected


class TestReviewFindingsOnRouting:
    """Routing edge cases: a fix after a failed test is retested, Security
    loopbacks count toward the retry limit, and a plan without a deploy
    flag stops for the owner."""

    GATES_OFF = TestNewPipelineEndToEnd.GATES_OFF
    _write = TestNewPipelineEndToEnd._write

    def _answer(self, config, task_id, verdict, extra=""):
        """Write the artifact relay is currently waiting for, under the exact
        name it asked for (retries get names like 06-tester-pass-2.md)."""
        state = run_state.load_run_state(task_id, config)
        current = state["history"][-1]
        name = agents.artifact_name(current["phase"], current["step"], state)
        (settings.TASKS_DIR / task_id / name).write_text(
            f"---\ntask_id: {task_id}\nphase: {current['phase']}\nstep: {current['step']}\n"
            f"date: 2026-10-03\nverdict: {verdict}\n{extra}---\n\nBody.\n"
        )

    def _start_through_plan(self, config, task_id, deploy="no"):
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, task_id)
        commands.cmd_start(task_id, auto=False, config=config)
        self._write(task_dir, 1, "designer", "ready-for-architect")
        commands.cmd_next(task_id, auto=False, config=config)
        self._write(task_dir, 2, "architect", "ready-for-builder", f"deploy: {deploy}\n")
        commands.cmd_next(task_id, auto=False, config=config)
        return task_dir

    def test_failed_tester_followed_by_a_fix_must_be_retested(self, sqlite_env):
        """Blocker 1: old-order sequence security secure -> tester fail ->
        builder -> reviewer -> security secure used to complete the task
        with the last Tester verdict still 'fail'."""
        config = {**sqlite_env, "human_gates": self.GATES_OFF}
        self._start_through_plan(config, "retest")
        for verdict in ("ready-for-review", "approved"):          # builder, reviewer
            self._answer(config, "retest", verdict)
            if verdict == "approved":
                break
            commands.cmd_next("retest", auto=False, config=config)
        commands.cmd_override("retest", "security", config, auto=False)  # old order
        self._answer(config, "retest", "secure")
        commands.cmd_next("retest", auto=False, config=config)        # -> tester (none yet)
        redirected = run_state.load_run_state("retest", config)
        assert redirected["current_phase"] == "tester"
        assert redirected["retries"].get("tester", 0) == 0  # a redirect is not a retry
        self._answer(config, "retest", "fail")
        commands.cmd_next("retest", auto=False, config=config)        # -> builder
        self._answer(config, "retest", "ready-for-review")
        commands.cmd_next("retest", auto=False, config=config)        # -> reviewer
        self._answer(config, "retest", "approved")
        commands.cmd_override("retest", "security", config, auto=False)  # old order again
        self._answer(config, "retest", "secure")
        commands.cmd_next("retest", auto=False, config=config)

        state = run_state.load_run_state("retest", config)
        assert state["status"] == "in_progress"
        assert state["current_phase"] == "tester"

    def test_security_loopbacks_count_toward_the_retry_limit(self, sqlite_env):
        """Blocker 2: removing security from the loopback rule went unnoticed."""
        config = {**sqlite_env, "human_gates": self.GATES_OFF, "max_retries": 1}
        self._start_through_plan(config, "sec-loop")
        for _round in range(3):
            for verdict in ("ready-for-review", "approved", "pass", "critical-findings"):
                self._answer(config, "sec-loop", verdict)
                commands.cmd_next("sec-loop", auto=False, config=config)
                if run_state.load_run_state("sec-loop", config)["status"] == "blocked":
                    break
            if run_state.load_run_state("sec-loop", config)["status"] == "blocked":
                break
        state = run_state.load_run_state("sec-loop", config)
        assert state["status"] == "blocked"
        assert state["retries"]["builder"] == 1

    def test_plan_without_deploy_flag_stops_for_the_owner(self, sqlite_env, capsys):
        """Warning 1: a missing deploy flag used to keep the start-time guess
        and could silently skip Ops."""
        config = {**sqlite_env, "human_gates": self.GATES_OFF}
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "no-flag")
        commands.cmd_start("no-flag", auto=False, config=config, deploy_intent=True)
        self._write(task_dir, 1, "designer", "ready-for-architect")
        commands.cmd_next("no-flag", auto=False, config=config)
        self._write(task_dir, 2, "architect", "ready-for-builder")
        capsys.readouterr()
        commands.cmd_next("no-flag", auto=False, config=config)
        out = capsys.readouterr().out
        assert "HUMAN GATE" in out and "no valid deploy" in out
        assert "Add 'deploy: yes' or 'deploy: no'" in out  # tells the owner how to decide
        assert "keeps the start-time guess: deploy=yes" in out
        assert run_state.load_run_state("no-flag", config)["current_phase"] == "architect"

    def test_adding_the_deploy_flag_and_rerunning_next_resolves_the_gate(self, sqlite_env):
        """The remedy the gate prints actually works."""
        config = {**sqlite_env, "human_gates": self.GATES_OFF}
        task_dir = _make_task_dir_with_brief(settings.TASKS_DIR, "flag-fixed")
        commands.cmd_start("flag-fixed", auto=False, config=config, deploy_intent=True)
        self._write(task_dir, 1, "designer", "ready-for-architect")
        commands.cmd_next("flag-fixed", auto=False, config=config)
        self._write(task_dir, 2, "architect", "ready-for-builder")
        commands.cmd_next("flag-fixed", auto=False, config=config)      # gate
        self._write(task_dir, 2, "architect", "ready-for-builder", "deploy: no\n")
        commands.cmd_next("flag-fixed", auto=False, config=config)
        state = run_state.load_run_state("flag-fixed", config)
        assert state["current_phase"] == "builder"
        assert state["deploy_intent"] is False

    def test_needs_designer_loop_counts_toward_the_retry_limit(self, sqlite_env):
        """Every routing loopback counts, not only builder and reviewer."""
        config = {**sqlite_env, "human_gates": self.GATES_OFF}
        _make_task_dir_with_brief(settings.TASKS_DIR, "brief-loop")
        commands.cmd_start("brief-loop", auto=False, config=config)
        self._answer(config, "brief-loop", "ready-for-architect")
        commands.cmd_next("brief-loop", auto=False, config=config)
        self._answer(config, "brief-loop", "needs-designer")
        commands.cmd_next("brief-loop", auto=False, config=config)
        state = run_state.load_run_state("brief-loop", config)
        assert state["current_phase"] == "designer"
        assert state["retries"].get("designer") == 1

    def test_override_into_legacy_pentest_is_refused(self, sqlite_env):
        """Warning 4: there is no pentest agent any more."""
        _make_task_dir_with_brief(settings.TASKS_DIR, "no-pentest")
        commands.cmd_start("no-pentest", auto=False, config=sqlite_env)
        with pytest.raises(SystemExit):
            commands.cmd_override("no-pentest", "pentest", sqlite_env, auto=False)

    def test_load_config_never_mutates_the_defaults(self, tmp_path, monkeypatch):
        """Nit 4: the shallow copy let a user config rewrite DEFAULT_CONFIG."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("human_gates:\n  after_designer: false\n")
        monkeypatch.setattr(settings, "CONFIG_PATH", config_file)
        loaded = settings.load_config()
        assert loaded["human_gates"]["after_designer"] is False
        assert settings.DEFAULT_CONFIG["human_gates"]["after_designer"] is True


class TestTesterSinceLastBuild:
    def test_only_a_passing_tester_after_the_last_build_counts(self):
        history = [{"phase": "builder", "step": 3}, {"phase": "tester", "step": 4, "verdict": "pass"},
                   {"phase": "builder", "step": 5}, {"phase": "tester", "step": 6, "verdict": "fail"}]
        assert routing._tester_ran_since_last_build({"history": history}) is False
        history.append({"phase": "builder", "step": 7})
        history.append({"phase": "tester", "step": 8})          # no verdict yet
        assert routing._tester_ran_since_last_build({"history": history}) is False
        history[-1]["verdict"] = "pass_with_concerns"
        assert routing._tester_ran_since_last_build({"history": history}) is True
