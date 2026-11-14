"""
Relay SQLite persistence layer.

Canonical orchestration state lives here once `persistence_backend: sqlite`
is set in config.yaml. Markdown/YAML (run.yaml, artifact frontmatter) become
generated projections of this database, never written to directly by the
sqlite-backed code paths in Relay's other modules.

Design:

  tasks  -- one row per task, the current-state snapshot. Never hand-edited;
            always written by record_step()/save_task_state() inside a
            transaction, mirroring what save_run_state() does for run.yaml
            on the files backend.
  steps  -- one row per numbered agent artifact (phase, step, verdict,
            filename, content hash). Routing reads the latest artifact's
            verdict, not run.yaml; the steps table holds the same facts.
  events -- insert-only audit log. Anything steps/tasks can't capture on
            its own (retries, human overrides/approvals, worktree lifecycle,
            raw state saves) gets an event row here. This is deliberately
            not full event sourcing: tasks/steps are still the fast-path
            read model; events exist so "what happened to this task and
            when" is a query, not a forensic file read.

This module imports none of Relay's other modules when it loads but
bounded_yaml, which is standalone (no circular import; one function imports
agents.artifact_name late). The caller
(settings.py) calls configure() before opening the ledger, with its VALID_PHASES /
VALID_VERDICTS / VALID_STATUSES so there is exactly one place those enums
are defined -- this module enforces them, it does not redefine them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - settings.py already fails fast on this
    _yaml = None

import bounded_yaml


SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,
    repo            TEXT,
    created         TEXT NOT NULL,
    updated         TEXT NOT NULL,
    status          TEXT NOT NULL,
    current_phase   TEXT NOT NULL,
    next_step       INTEGER NOT NULL DEFAULT 1,
    deploy_intent   INTEGER NOT NULL DEFAULT 0,
    worktree_path   TEXT,
    branch          TEXT,
    base            TEXT,
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS steps (
    task_id             TEXT NOT NULL REFERENCES tasks(task_id),
    step                INTEGER NOT NULL,
    phase               TEXT NOT NULL,
    verdict             TEXT,
    artifact_filename   TEXT NOT NULL,
    content_hash        TEXT,
    created_at          TEXT,  -- nullable: a migrated step's real timestamp may
                                -- genuinely be unrecoverable from the legacy
                                -- corpus; NULL means "unknown", never fabricated.
                                -- record_step_and_save() (the live save path,
                                -- not migration) always supplies a real value.
    PRIMARY KEY (task_id, step)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id),
    event_type  TEXT NOT NULL,
    phase       TEXT,
    step        INTEGER,
    verdict     TEXT,
    payload     TEXT,
    timestamp   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(task_id, event_type);
CREATE INDEX IF NOT EXISTS idx_steps_task  ON steps(task_id, step);
"""

# Event types actually used. Not an enforced enum (events is intentionally a
# loose audit log), just documentation of the vocabulary in use.
EVENT_TASK_CREATED = "task_created"
EVENT_STATE_SAVED = "state_saved"
EVENT_RETRY = "retry"
EVENT_APPROVAL = "approval"
EVENT_WORKTREE_CREATED = "worktree_created"
EVENT_WORKTREE_FINALIZED = "worktree_finalized"
EVENT_MIGRATION_OVERRIDE = "migration_override"
EVENT_HISTORICAL_NOTE = "historical_note"
EVENT_STEP_NEVER_DELIVERED = "step_never_delivered"
EVENT_DEPLOY_DECIDED = "deploy_decided"
EVENT_SENT_BACK = "sent_back"
EVENT_TEST_RUN = "test_run"
EVENT_TEST_COMMAND_ACCEPTED = "test_command_accepted"
EVENT_SLEPT_DURING_STEP = "slept_during_step"
EVENT_OWNER_NOTE = "owner_note"
# What `relay start` read of the task's size, risk and deploy (sizing.py).
EVENT_SIZED = "sized"
# Relay's checks on a role's report. Ledgers written by older versions still
# hold this event; nothing writes or reads it.
EVENT_REPORT_CHECK = "report_check"
# The process audit. Ledgers written by older versions still hold these two
# events; nothing writes them and every reader passes over them.
EVENT_AUDIT_STARTED = "audit_started"
EVENT_AUDIT_REPORTED = "audit_reported"


class InvalidTransition(ValueError):
    """Raised when a phase/verdict outside the configured enums is persisted.

    This is a persistence-layer integrity guard, independent of and in
    addition to routing.py's own determine_next()/ROUTING logic -- it does not
    replace that logic, it backstops it at the point of writing.
    """


class DatabaseUnavailable(RuntimeError):
    """Base class: the canonical sqlite database cannot be used as-is.

    Raised only by connect_existing() -- the fail-closed entry point real
    Relay commands use once persistence_backend is sqlite. connect()
    (used for deliberate creation, and throughout the test suite for
    low-level plumbing) never raises this; it happily creates a fresh
    database, exactly as it always has. The distinction is the point:
    ordinary command execution must never silently fabricate or repair
    canonical state, only an explicit, operator-invoked action may.
    """


class DatabaseMissing(DatabaseUnavailable):
    """The expected canonical database file does not exist."""


class DatabaseCorrupt(DatabaseUnavailable):
    """The database file exists but failed an integrity check, or isn't a
    valid sqlite database at all."""


class MigrationExcluded(ValueError):
    """Raised when import_task() is called for a task an explicit
    MigrationDecision marks exclude=True. A distinct exception from
    InvalidTransition -- exclusion is a deliberate human decision, not a
    data-integrity failure."""


# Python: @dataclass writes __init__, __eq__ and __repr__ for you from the field
#   list below. frozen=True makes instances read-only after creation, so a
#   decision can't be quietly changed after it's validated.
@dataclasses.dataclass(frozen=True)
class StepVerdictOverride:
    """An explicit, human-authorized correction for ONE step's verdict.

    Deliberately narrow: names the exact step, the exact historical value
    expected to be found there, and the exact canonical value it becomes.
    It is not "set this step's verdict to X" -- it is "if this step's
    verdict is still exactly Y, treat it as X" -- so a decision authored
    against one artifact can never silently apply to a different one that
    happens to share a step number, and a decision that no longer matches
    what's on disk fails loudly instead of applying blind.
    """
    step: int
    expected_verdict: str
    canonical_verdict: str
    note: str = ""

    # Python: __post_init__ runs right after the generated __init__. It's the place
    #   for validation that needs several fields at once (raise to reject).
    def __post_init__(self):
        if not self.note:
            raise ValueError(f"StepVerdictOverride for step {self.step} must include an audit note")


@dataclasses.dataclass(frozen=True)
class MigrationDecision:
    """An explicit, human-authorized mapping for one task's migration.

    This is the *entire* migration-override mechanism -- narrowly typed on
    purpose, not a generic dict of arbitrary field overrides. At the task
    level it can only ever map a legacy value to a canonical status, a
    canonical current_phase, or "exclude this task from migration
    entirely." At the step level (`step_overrides`) it can only correct
    one named step's verdict, and only when the historical value found
    there still matches exactly what the decision expects. Nothing else
    is expressible. It is never constructed, read, or reachable from
    record_step_and_save(), create_task() without an explicit
    current_phase argument, or any cmd_* function in commands.py -- ordinary
    Relay operation has no code path that could produce or accept one, so
    it cannot be activated by accident.

    Every decision (and every step override within it) requires a `note`
    -- there is no silent, unexplained override. import_task() validates
    status/current_phase/verdicts against the same canonical enums as
    everything else before writing them; a MigrationDecision can point at
    a wrong-but-plausible-looking value and it will still be rejected at
    write time, exactly like a raw legacy value would be. The decision
    doesn't bypass validation -- it's the only way to supply a
    *replacement* value for validation to check.
    """
    task_id: str
    status: str | None = None
    current_phase: str | None = None
    exclude: bool = False
    note: str = ""
    step_overrides: tuple["StepVerdictOverride", ...] = ()
    # Step numbers a human has confirmed were never delivered (a gap in the
    # artifact trail). Recorded as events; never filled in or fabricated.
    missing_steps: tuple[int, ...] = ()
    # Files in the agent-artifact numbering range that are not agent phase
    # output (prose plans, no-frontmatter docs, a second file reusing a step
    # number). Imported as historical notes, never as pipeline steps.
    note_files: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.note:
            raise ValueError(f"MigrationDecision for '{self.task_id}' must include an audit note")
        if self.exclude and (self.status is not None or self.current_phase is not None
                              or self.step_overrides or self.missing_steps or self.note_files):
            raise ValueError(
                f"MigrationDecision for '{self.task_id}' cannot both exclude the task "
                f"and override its fields -- exclusion means nothing else is imported"
            )
        for field_name, values in (("missing_steps", self.missing_steps),
                                   ("note_files", self.note_files)):
            if len(values) != len(set(values)):
                raise ValueError(
                    f"MigrationDecision for '{self.task_id}' lists the same {field_name} entry "
                    f"more than once"
                )
        steps_seen = [so.step for so in self.step_overrides]
        if len(steps_seen) != len(set(steps_seen)):
            raise ValueError(
                f"MigrationDecision for '{self.task_id}' has more than one step_override "
                f"for the same step -- each step gets exactly one explicit decision"
            )

    def step_override_for(self, step: int) -> "StepVerdictOverride | None":
        for so in self.step_overrides:
            if so.step == step:
                return so
        return None


# ---------------------------------------------------------------------------
# Configuration -- caller supplies the state-machine enums it already owns.
# ---------------------------------------------------------------------------

_valid_phases: list[str] = []
_valid_verdicts: dict[str, list[str]] = {}
_valid_statuses: list[str] = []


def configure(valid_phases: list[str], valid_verdicts: dict[str, list[str]],
              valid_statuses: list[str]) -> None:
    """Register the state-machine enums this module validates against.

    Called by settings.py before the ledger is opened, with its VALID_PHASES /
    VALID_VERDICTS / VALID_STATUSES so there is a single source of truth
    for what a valid phase/verdict/status is -- this module never defines
    its own copy.
    """
    global _valid_phases, _valid_verdicts, _valid_statuses
    _valid_phases = list(valid_phases)
    _valid_verdicts = dict(valid_verdicts)
    _valid_statuses = list(valid_statuses)


def _require_configured():
    if not _valid_phases:
        raise RuntimeError("db.configure() must be called before use")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def connect(db_path: Path) -> sqlite3.Connection:
    """Open db_path, creating a fresh empty database (and its parent dir)
    if nothing is there yet. Used for deliberate creation (init_database())
    and by the test suite's low-level plumbing. Relay's real production
    connection path uses connect_existing() instead -- see its docstring
    for why the two must not be merged."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def connect_existing(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection to a database that must already exist and be
    valid. This is Relay's real, production entry point once
    persistence_backend is sqlite -- it never creates a new file and never
    silently accepts a corrupt one.

    Fails closed on purpose: a missing or corrupt canonical database means
    Relay's actual orchestration state may be lost, and "guess and
    continue" (e.g. quietly starting from an empty database, or from the
    Markdown/YAML projections) is exactly the failure mode the original
    persistence-v2 brief ruled out. Recovery is always an explicit,
    separate, operator-invoked step (see the `recover` command in commands.py),
    never an implicit side effect of an ordinary command failing to find
    what it expected.

    read_only=True is for the read API (relay status|list|gate-check
    --json): the same missing/corrupt checks, then a connection SQLite
    itself refuses to write through.
    """
    if not db_path.exists():
        raise DatabaseMissing(
            f"No canonical database found at {db_path}. Relay will not silently "
            "create a new one -- persistence_backend=sqlite means this file is "
            "the single source of truth for orchestration state. If this is "
            "genuinely a first-time setup, run 'relay recover --init'. If a "
            "database used to be here, restore it from backup before continuing."
        )

    # Probe with a read-only, immutable connection FIRST, before ever
    # opening a normal read-write one. A plain read-write open of a
    # database with a stale/unmerged -wal or -journal file (left behind by
    # a crash) triggers SQLite's automatic recovery replay as a side
    # effect of opening it -- which writes to the main file. That is the
    # opposite of what a corruption check should do to a file that might
    # need to be preserved untouched for inspection or backup.
    # immutable=1 tells SQLite the file will not change and skips that
    # recovery machinery entirely, so a corrupt/unhealthy database is
    # genuinely never mutated by this check.
    probe_uri = f"file:{db_path}?mode=ro&immutable=1"
    try:
        probe = sqlite3.connect(probe_uri, uri=True, timeout=5)
        try:
            integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            probe.close()
    except sqlite3.Error as e:
        raise DatabaseCorrupt(
            f"Database at {db_path} could not be read as a valid sqlite database "
            f"({e}). Relay will not operate against it, and has not modified it. "
            "Restore from backup, or run 'relay recover --from-projections' to "
            "review what a rebuild from Markdown/YAML projections would look "
            "like before applying it."
        ) from e
    if integrity != "ok":
        raise DatabaseCorrupt(
            f"Database at {db_path} failed integrity check: {integrity}. Relay "
            "will not operate against a corrupt canonical database, and has not "
            "modified it. Restore from backup, or run 'relay recover "
            "--from-projections' to review what a rebuild from Markdown/YAML "
            "projections would look like before applying it."
        )

    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # Only now, having confirmed the file is healthy, open the real
    # read-write connection this process will actually use.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def init_database(db_path: Path) -> sqlite3.Connection:
    """Explicitly create (or open, idempotently) the canonical database at
    db_path. This is the only sanctioned way a new database file comes
    into existence for the sqlite backend in real use -- an operator's
    deliberate decision (via 'relay recover --init'), never an implicit
    side effect of an ordinary command. Safe to call again later: opening
    an already-existing, healthy database is a no-op beyond the idempotent
    schema init connect() already does.
    """
    return connect(db_path)


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent. Safe to call on every connect()."""
    conn.executescript(_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

def task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return row is not None


def _insert_task_row(conn: sqlite3.Connection, task_id: str, *, repo: str | None,
                      deploy_intent: bool, current_phase: str) -> None:
    """The actual INSERT + task_created event, with no transaction
    management of its own -- the caller owns the transaction boundary.

    This exists so create_task() (a standalone, self-committing call) and
    import_task() (which needs task-row creation to be part of ONE larger
    transaction alongside its steps/events/history writes: see the
    comment in import_task) share the exact same insert logic instead of import_task
    re-implementing it or calling a self-committing create_task() and
    silently losing atomicity across the two.
    """
    _require_configured()
    if current_phase not in _valid_phases and current_phase != "done":
        raise InvalidTransition(f"cannot create task in unknown phase '{current_phase}'")
    now = _now()
    conn.execute(
        """INSERT INTO tasks
           (task_id, repo, created, updated, status, current_phase,
            next_step, deploy_intent, worktree_path, branch, base, schema_version)
           VALUES (?, ?, ?, ?, 'in_progress', ?, 1, ?, NULL, NULL, NULL, ?)""",
        (task_id, repo, now, now, current_phase, int(deploy_intent), SCHEMA_VERSION),
    )
    conn.execute(
        "INSERT INTO events (task_id, event_type, phase, timestamp) VALUES (?, ?, ?, ?)",
        (task_id, EVENT_TASK_CREATED, current_phase, now),
    )


def create_task(conn: sqlite3.Connection, task_id: str, *, repo: str | None = None,
                 deploy_intent: bool = False, current_phase: str = "architect") -> dict:
    """Create a new task row. Mirrors init_run_state()'s defaults exactly.

    "done" is accepted alongside the real phases -- it is the same sentinel
    Relay itself stores in run.yaml's current_phase once a workflow
    completes (see cmd_next's "Workflow complete" branch and cmd_override's
    `allowed = VALID_PHASES + ["done"]`), not a real phase with steps/verdicts
    of its own. record_step_and_save() makes the same exception, so
    import_task() can carry a legacy task's real "done" phase through.

    Standalone callers of create_task() get their own self-contained
    transaction (this function's own `with conn:`). import_task() does not
    call this function directly: see the comment in import_task for why
    (atomicity across task creation and the rest of the import).
    """
    with conn:
        _insert_task_row(conn, task_id, repo=repo, deploy_intent=deploy_intent,
                          current_phase=current_phase)
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """Reconstruct the same shape load_run_state() returns from run.yaml.

    retries{} is derived from the task's events (see get_retry_counts),
    never stored as a separately-mutable counter. history[] is derived from the steps table,
    not stored redundantly on the tasks row.
    """
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    state = {
        "task_id": row["task_id"],
        "created": row["created"],
        "updated": row["updated"],
        "status": row["status"],
        "current_phase": row["current_phase"],
        "next_step": row["next_step"],
        "schema_version": row["schema_version"],
        "repo": row["repo"],
        "deploy_intent": bool(row["deploy_intent"]),
        "worktree_path": row["worktree_path"],
        "branch": row["branch"],
        "base": row["base"],
        "retries": get_retry_counts(conn, task_id),
        "history": get_history(conn, task_id),
    }
    return state


def list_tasks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT task_id FROM tasks ORDER BY task_id").fetchall()
    return [get_task(conn, r["task_id"]) for r in rows]


def get_history(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """One history entry per step row, matching record_phase()'s shape:
    {phase, step, timestamp, verdict?}."""
    rows = conn.execute(
        "SELECT step, phase, verdict, created_at FROM steps WHERE task_id = ? ORDER BY step",
        (task_id,),
    ).fetchall()
    history = []
    for r in rows:
        entry = {"phase": r["phase"], "step": r["step"], "timestamp": r["created_at"]}
        if r["verdict"]:
            entry["verdict"] = r["verdict"]
        history.append(entry)
    return history


# Python: dict[str, int] is a type hint: keys are str, values are int. Hints
#   aren't enforced at runtime; they document intent for readers and tools.
def get_retry_counts(conn: sqlite3.Connection, task_id: str) -> dict[str, int]:
    """Automatic rounds into each phase since the owner last decided, read
    from the task's events in the order they were written.

    A retry event (Relay routed the task back on its own) adds one to its
    phase. A send-back or an override into a phase is the owner's decision
    and starts that phase's count again; so does an override out of a stop
    at the limit, for the phase the limit stopped. Nothing is stored: a ledger written
    before this rule is read by it too, and there a send-back's own retry
    event sits just before its sent_back event, which clears it.

    builder and reviewer are always present (as they were before every
    routing loopback counted) so existing projections stay identical."""
    counts = {"builder": 0, "reviewer": 0}
    rows = conn.execute(
        # Python: The ? placeholders are parameter binding: sqlite3 inserts the values
        #   safely, so a task id can never be read as SQL (no injection).
        "SELECT event_type, phase, payload FROM events "
        "WHERE task_id=? AND event_type IN (?,?,?) ORDER BY id",
        (task_id, EVENT_RETRY, EVENT_SENT_BACK, EVENT_APPROVAL),
    ).fetchall()
    for event_type, phase, payload in rows:
        if event_type == EVENT_RETRY:
            counts[phase] = counts.get(phase, 0) + 1
            continue
        for decided_phase in _phases_the_owner_decided(event_type, payload):
            if decided_phase in counts:
                counts[decided_phase] = 0
    return counts


def _phases_the_owner_decided(event_type: str, payload: str | None) -> list[str]:
    """The phases whose count a sent_back or approval event starts again:
    the one the owner moved the task into and, for an override out of a stop
    at the limit, the one the limit had stopped. Empty when the event is not
    the owner moving the task (an approval that is not an override)."""
    try:
        details = json.loads(payload) if payload else {}
    except (ValueError, RecursionError):
        return []  # an unreadable payload decides nothing, and one nested too deep is unreadable
    if not isinstance(details, dict):
        return []
    if event_type == EVENT_SENT_BACK:
        phases = [details.get("to_phase")]
    elif details.get("method") == "override":
        phases = [details.get("next_phase"), details.get("stopped_phase")]
    else:
        return []
    return [phase for phase in phases if isinstance(phase, str)]  # so does a phase that is not text


# ---------------------------------------------------------------------------
# Steps -- the replacement for find_latest_artifact()/parse_frontmatter()
# ---------------------------------------------------------------------------

def get_latest_step(conn: sqlite3.Connection, task_id: str, task_dir: Path | None = None) -> dict | None:
    """The routing-relevant replacement for find_latest_artifact(). Returns
    a frontmatter-shaped dict {task_id, phase, step, verdict, filename} for
    the highest-numbered step, or None if the task has no steps yet.

    A step row is created with verdict=NULL when a phase starts (mirroring
    record_phase()'s "no verdict yet" state) -- the verdict itself is only
    known once the agent has actually written its artifact file. So when
    the stored verdict is still NULL and task_dir is given, this makes the
    one deliberate, transitional filesystem read the architecture calls
    for: it checks whether the artifact file now exists with a verdict in
    its frontmatter, and if so, syncs that single row before returning.
    This is a targeted read of one known filename, not a directory scan --
    it does not reintroduce find_latest_artifact()'s file-scan-and-sort.
    """
    row = conn.execute(
        "SELECT * FROM steps WHERE task_id = ? ORDER BY step DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    verdict = row["verdict"]
    if verdict is None and task_dir is not None:
        synced = sync_step_verdict_from_file(conn, task_id, row["step"], row["phase"],
                                              row["artifact_filename"], task_dir)
        if synced is not None:
            verdict = synced

    return {
        "task_id": task_id,
        "phase": row["phase"],
        "step": row["step"],
        "verdict": verdict,
        "artifact_filename": row["artifact_filename"],
    }


def sync_step_verdict_from_file(conn: sqlite3.Connection, task_id: str, step: int,
                                 phase: str, artifact_filename: str, task_dir: Path) -> str | None:
    """Read one known artifact file's frontmatter and, if it now carries a
    verdict the steps table doesn't have yet, persist it. Returns the
    verdict if one was found and synced (or already present), else None.

    Validates against the configured VALID_VERDICTS before writing -- an
    artifact with a malformed or unrecognized verdict, or a frontmatter
    that cannot be read, is left unsynced
    (verdict stays NULL in the DB) rather than persisting bad data; the
    existing "No verdict found" warning path in cmd_next (commands.py) then
    fires exactly as it does under the files backend.
    """
    _require_configured()
    path = task_dir / artifact_filename
    if not path.exists():
        return None
    text = path.read_text()
    try:
        fm = _parse_frontmatter_text(text)
    except ValueError:  # a frontmatter that cannot be read: no verdict, as with none
        return None
    verdict = fm.get("verdict")
    if not verdict or verdict not in _valid_verdicts.get(phase, []):
        return None
    content_hash = _hash_file(path)
    with conn:
        conn.execute(
            "UPDATE steps SET verdict=?, content_hash=? WHERE task_id=? AND step=?",
            (verdict, content_hash, task_id, step),
        )
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, verdict, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, "verdict_recorded", phase, step, verdict, _now()),
        )
    return verdict


def get_step_artifacts(conn: sqlite3.Connection, task_id: str) -> dict[int, str]:
    """The artifact file name recorded for each step, by step number. Kept
    out of get_history() on purpose: that feeds run.yaml, which has no
    such field."""
    rows = conn.execute(
        "SELECT step, artifact_filename FROM steps WHERE task_id = ? ORDER BY step",
        (task_id,),
    ).fetchall()
    return {r["step"]: r["artifact_filename"] for r in rows}


def get_next_step_number(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(step) AS m FROM steps WHERE task_id = ?", (task_id,)
    ).fetchone()
    return (row["m"] or 0) + 1


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_step_and_save(conn: sqlite3.Connection, task_id: str, state: dict,
                          *, task_dir: Path | None = None) -> None:
    """The sqlite-backend replacement for save_run_state().

    Relay's existing call sites mutate a run-state dict in place
    (init_run_state / record_phase / direct field assignment) and then call
    save_run_state(task_id, state) once. This function is the drop-in
    sqlite equivalent: it upserts the tasks row from the dict exactly as
    save_run_state() would write run.yaml, then reconciles state["history"]
    against the steps table -- inserting any step not yet recorded, and
    backfilling a verdict onto an existing step row when cmd_next's
    "attach verdict to the matching history entry" logic has just done that
    in memory. retries[] in the dict is never written directly (see
    get_retry_counts) -- a retry event is appended only when a phase's count
    in the dict is above the count the ledger's events give, which is exactly
    the +1-at-a-time pattern increment_retry() produces. A count set back to
    zero writes nothing here: the sent_back or approval event is the reset.

    All of this happens in a single transaction: a state save either lands
    completely, or not at all.
    """
    _require_configured()
    phase = state.get("current_phase")
    if phase and phase != "done" and phase not in _valid_phases:
        raise InvalidTransition(f"cannot save task in unknown phase '{phase}'")
    status = state.get("status")
    if status and status not in _valid_statuses:
        raise InvalidTransition(f"cannot save task with unknown status '{status}'")

    now = state.get("updated") or _now()
    existing = get_task(conn, task_id)

    with conn:
        if existing is None:
            conn.execute(
                """INSERT INTO tasks (task_id, repo, created, updated, status,
                       current_phase, next_step, deploy_intent, worktree_path,
                       branch, base, schema_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, state.get("repo"), state.get("created", now), now,
                 status or "in_progress", phase or "architect",
                 state.get("next_step", 1), int(bool(state.get("deploy_intent"))),
                 state.get("worktree_path"), state.get("branch"), state.get("base"),
                 state.get("schema_version", SCHEMA_VERSION)),
            )
            conn.execute(
                "INSERT INTO events (task_id, event_type, phase, timestamp) VALUES (?,?,?,?)",
                (task_id, EVENT_TASK_CREATED, phase, now),
            )
        else:
            conn.execute(
                """UPDATE tasks SET repo=?, updated=?, status=?, current_phase=?,
                       next_step=?, deploy_intent=?, worktree_path=?, branch=?, base=?
                   WHERE task_id=?""",
                (state.get("repo"), now, status or existing["status"],
                 phase or existing["current_phase"], state.get("next_step", existing["next_step"]),
                 int(bool(state.get("deploy_intent"))), state.get("worktree_path"),
                 state.get("branch"), state.get("base"), task_id),
            )
            payload = json.dumps({
                "status": [existing["status"], status],
                "current_phase": [existing["current_phase"], phase],
            })
            conn.execute(
                "INSERT INTO events (task_id, event_type, phase, payload, timestamp) VALUES (?,?,?,?,?)",
                (task_id, EVENT_STATE_SAVED, phase, payload, now),
            )

        # Reconcile history[] against steps.
        known_steps = {
            r["step"]: r["verdict"]
            for r in conn.execute("SELECT step, verdict FROM steps WHERE task_id = ?", (task_id,))
        }
        for entry in state.get("history", []):
            step = entry.get("step")
            if step is None:
                continue
            entry_phase = entry.get("phase")
            entry_verdict = entry.get("verdict")
            if entry_phase not in _valid_phases:
                raise InvalidTransition(f"cannot record step {step} in unknown phase '{entry_phase}'")
            if entry_verdict and entry_verdict not in _valid_verdicts.get(entry_phase, []):
                raise InvalidTransition(
                    f"cannot record verdict '{entry_verdict}' for phase '{entry_phase}'"
                )
            if step not in known_steps:
                from agents import artifact_name  # local import: avoids a cycle at module load
                filename = artifact_name(entry_phase, step, state)
                content_hash = None
                if task_dir is not None:
                    content_hash = _hash_file(task_dir / filename)
                conn.execute(
                    """INSERT INTO steps (task_id, step, phase, verdict, artifact_filename,
                           content_hash, created_at) VALUES (?,?,?,?,?,?,?)""",
                    (task_id, step, entry_phase, entry_verdict, filename, content_hash,
                     entry.get("timestamp", now)),
                )
            elif entry_verdict and known_steps[step] != entry_verdict:
                content_hash = None
                if task_dir is not None:
                    from agents import artifact_name
                    filename = artifact_name(entry_phase, step, state)
                    content_hash = _hash_file(task_dir / filename)
                conn.execute(
                    "UPDATE steps SET verdict=?, content_hash=COALESCE(?, content_hash) WHERE task_id=? AND step=?",
                    (entry_verdict, content_hash, task_id, step),
                )

        # Retry events: append one per +1 over the count the events give.
        stored_counts = get_retry_counts(conn, task_id)
        for retry_phase, count in (state.get("retries") or {}).items():
            for _ in range(max(0, count - stored_counts.get(retry_phase, 0))):
                conn.execute(
                    "INSERT INTO events (task_id, event_type, phase, timestamp) VALUES (?,?,?,?)",
                    (task_id, EVENT_RETRY, retry_phase, now),
                )


# ---------------------------------------------------------------------------
# Approvals / worktree lifecycle -- thin event annotations, not new subsystems
# ---------------------------------------------------------------------------

def record_approval(conn: sqlite3.Connection, task_id: str, *, phase: str,
                     next_phase: str | None, method: str, note: str | None = None,
                     stopped_phase: str | None = None) -> None:
    """Record that a human approved a phase transition (relay override, or
    a completed human gate). This does not grant any authority itself -- it
    is a record of an authorization decision the human already made by
    running the command. This is deliberately the whole approval
    subsystem, not a framework.

    `note` is what the owner said while approving, if anything; it is handed
    to every later agent (see owner_notes.py). `stopped_phase` is the phase
    the limit on automatic rounds had stopped when the owner overrode: the
    override is the owner's decision, so that phase's count starts again
    wherever the task goes (see get_retry_counts). The key is written only
    then, and a version from before it ignores it.
    """
    details = {"next_phase": next_phase, "method": method, "note": note}
    if stopped_phase:
        details["stopped_phase"] = stopped_phase
    payload = json.dumps(details)
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, payload, timestamp) VALUES (?,?,?,?,?)",
            (task_id, EVENT_APPROVAL, phase, payload, _now()),
        )


def record_sent_back(conn: sqlite3.Connection, task_id: str, *, from_phase: str,
                     verdict: str, to_phase: str, note: str | None) -> None:
    """Record that the owner sent a finished step back for more work. This is
    deliberately not an approval: nothing was accepted. It also starts
    to_phase's count of automatic rounds again (see get_retry_counts)."""
    payload = json.dumps({"to_phase": to_phase, "note": note})
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, verdict, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_SENT_BACK, from_phase, verdict, payload, _now()),
        )


def record_test_run(conn: sqlite3.Connection, task_id: str, *, phase: str, step: int | None,
                    run: dict) -> None:
    """Record a test run Relay made itself after a phase finished: the
    commit, the command, how it came out and the counts. This is the
    evidence the roles and the routing rely on, in place of an agent's
    account of its own test results."""
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_TEST_RUN, phase, step, json.dumps(run), _now()),
        )


def record_test_command_accepted(conn: sqlite3.Connection, task_id: str, *, phase: str,
                                 step: int | None, command: str, test_report: str | None,
                                 commit: str | None) -> None:
    """Record that the owner accepted a test command the task's base branch
    does not have. Its own event, tied to the exact command and report
    path, so no other approval can stand in for it."""
    payload = json.dumps({"command": command, "test_report": test_report, "commit": commit})
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_TEST_COMMAND_ACCEPTED, phase, step, payload, _now()),
        )


def record_owner_note(conn: sqlite3.Connection, task_id: str, *, phase: str | None,
                      step: int | None, note: str) -> None:
    """Record something the owner said about the task without moving it:
    a decision, a correction, a fact. Later agents are given it."""
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_OWNER_NOTE, phase, step, json.dumps({"note": note}), _now()),
        )


def record_slept_during_step(conn: sqlite3.Connection, task_id: str, *, phase: str,
                             step: int | None, sleep: dict) -> None:
    """Record that the machine went to sleep while a step was open, so the
    time the step took can be read for what it was. `sleep` holds how many
    times it slept, for how long in all, and when the first and last began."""
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_SLEPT_DURING_STEP, phase, step, json.dumps(sleep), _now()),
        )


def record_deploy_decision(conn: sqlite3.Connection, task_id: str, *, step: int | None,
                           previous: bool, decided: bool) -> None:
    """Record the Architect's deploy decision when it changes the task's
    deploy intent, so the switch is part of the task's history."""
    payload = json.dumps({"previous": previous, "decided": decided, "source": "architect"})
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_DEPLOY_DECIDED, "architect", step, payload, _now()),
        )


def record_event(conn: sqlite3.Connection, task_id: str, event_type: str, payload: dict) -> None:
    """Record one event with a JSON payload and no phase or step."""
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
            (task_id, event_type, json.dumps(payload), _now()),
        )


def open_tasks_in(conn: sqlite3.Connection, worktree_path: str) -> list[str]:
    """Tasks still in progress (or blocked) that build in this checkout."""
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE worktree_path = ? AND status IN ('in_progress', 'blocked') "
        "ORDER BY task_id", (worktree_path,)).fetchall()
    return [row[0] for row in rows]


def record_worktree_event(conn: sqlite3.Connection, task_id: str, *, created: bool,
                           detail: str) -> None:
    event_type = EVENT_WORKTREE_CREATED if created else EVENT_WORKTREE_FINALIZED
    with conn:
        conn.execute(
            "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
            (task_id, event_type, json.dumps({"detail": detail}), _now()),
        )


def get_events(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT event_type, phase, step, verdict, payload, timestamp FROM events "
        "WHERE task_id = ? ORDER BY id", (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = {"event_type": r["event_type"], "phase": r["phase"], "step": r["step"],
             "verdict": r["verdict"], "timestamp": r["timestamp"]}
        if r["payload"]:
            d["payload"] = json.loads(r["payload"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Projections -- run.yaml regenerated from SQLite, never the other way round
# ---------------------------------------------------------------------------

def _dump_yaml(data: dict) -> str:
    return _yaml.dump(data, default_flow_style=False, sort_keys=False)


def generate_run_yaml(conn: sqlite3.Connection, task_id: str) -> str:
    """Byte-reproducible run.yaml text, generated purely from SQLite.

    Field order and shape match init_run_state()/save_run_state() exactly
    so a projected file is indistinguishable from a hand-written one.
    """
    state = get_task(conn, task_id)
    if state is None:
        raise ValueError(f"no such task: {task_id}")
    ordered = {
        "task_id": state["task_id"],
        "created": state["created"],
        "updated": state["updated"],
        "status": state["status"],
        "current_phase": state["current_phase"],
        "next_step": state["next_step"],
        "schema_version": state["schema_version"],
        "repo": state["repo"],
        "worktree_path": state["worktree_path"],
        "branch": state["branch"],
        "base": state["base"],
        "deploy_intent": state["deploy_intent"],
        "retries": state["retries"],
        "history": state["history"],
    }
    return _dump_yaml(ordered) + "\n"


def write_projection(conn: sqlite3.Connection, task_id: str, task_dir: Path) -> Path:
    """Write run.yaml to task_dir, atomically, from SQLite. Deleting this
    file and calling write_projection() again must reproduce it exactly --
    it carries zero information SQLite doesn't already have.
    """
    task_dir.mkdir(parents=True, exist_ok=True)
    target = task_dir / "run.yaml"
    tmp = target.with_suffix(".yaml.tmp")
    text = generate_run_yaml(conn, task_id)
    tmp.write_text(text)
    tmp.rename(target)
    return target


def projection_is_stale(conn: sqlite3.Connection, task_id: str, task_dir: Path) -> bool:
    """True if run.yaml on disk does not match what SQLite would generate.
    Comparison is content-based (not mtime-based), so a regenerated file
    with an identical byte stream is never flagged stale."""
    target = task_dir / "run.yaml"
    if not target.exists():
        return True
    return target.read_text() != generate_run_yaml(conn, task_id)


# ---------------------------------------------------------------------------
# Migration: dry-run + conflict detection against the real file corpus.
# No writes to the source files or to `conn` happen in this section --
# dry_run_migration() is read-only by construction.
# ---------------------------------------------------------------------------

def _parse_frontmatter_text(text: str) -> dict:
    """Frontmatter as a dict; {} when there is none. Raises ValueError when
    a frontmatter block exists but isn't a readable YAML mapping, or holds
    what bounded_yaml does not read."""
    try:
        return bounded_yaml.frontmatter(text)
    except bounded_yaml.NotRead as problem:
        raise ValueError(f"frontmatter {problem}") from problem


def read_file_state(task_dir: Path) -> dict:
    """Read a task's on-disk state exactly as the files backend in run_state.py does:
    run.yaml plus every numbered artifact's frontmatter. Read-only."""
    run_yaml_path = task_dir / "run.yaml"
    run_yaml = {}
    if run_yaml_path.exists():
        run_yaml = _yaml.safe_load(run_yaml_path.read_text()) or {}

    artifacts = []
    for f in sorted(task_dir.iterdir()) if task_dir.exists() else []:
        if f.suffix != ".md" or not f.name[0:2].isdigit():
            continue
        step_num = int(f.name[0:2])
        if step_num >= 90:
            continue  # human artifacts, not agent phase output
        frontmatter_error = None
        try:
            fm = _parse_frontmatter_text(f.read_text())
        except ValueError as error:
            fm, frontmatter_error = {}, str(error)
        artifacts.append({
            "frontmatter_error": frontmatter_error,
            "step": step_num,
            "filename": f.name,
            "phase": fm.get("phase"),
            "verdict": fm.get("verdict"),
            "date": fm.get("date"),  # day-granularity fallback timestamp source
            "content_hash": _hash_file(f),
        })
    artifacts.sort(key=lambda a: a["step"])
    return {"run_yaml": run_yaml, "artifacts": artifacts}


def reconcile_task(task_id: str, file_state: dict, *, check_step_sequence: bool = True,
                   check_phase_agreement: bool = True) -> dict:
    """Compare run.yaml against the artifact trail for one task, exactly as
    check_state_consistency() does, plus enum validation against the
    configured VALID_STATUSES/VALID_PHASES/VALID_VERDICTS.

    Never picks a winner. Returns {task_id, ok, discrepancies: [...]} --
    ok is False if anything disagrees or violates an enum; the caller
    decides what to do, this function only surfaces the finding.
    """
    _require_configured()
    discrepancies = []
    run_yaml = file_state["run_yaml"]
    artifacts = file_state["artifacts"]

    status = run_yaml.get("status")
    if status is not None and status not in _valid_statuses:
        discrepancies.append(f"run.yaml status '{status}' is not a valid status")

    ry_phase = run_yaml.get("current_phase")
    if ry_phase is not None and ry_phase not in _valid_phases + ["done"]:
        discrepancies.append(f"run.yaml current_phase '{ry_phase}' is not a valid phase")

    if artifacts:
        # Every artifact is examined, not merely the latest one: a bad
        # phase/verdict buried behind later, otherwise-clean steps (say
        # step 3 of 7) must be visible in the classification, not just at
        # import time, or the task would read as CLEAN.
        for art in artifacts:
            art_phase = art["phase"]
            if art.get("frontmatter_error"):
                discrepancies.append(
                    f"artifact ({art['filename']}) has unreadable frontmatter: "
                    f"{art['frontmatter_error']}"
                )
                continue
            if art_phase is None:
                discrepancies.append(
                    f"artifact ({art['filename']}) has no YAML frontmatter phase -- "
                    f"cannot tell which agent phase produced it"
                )
                continue
            if art_phase not in _valid_phases:
                discrepancies.append(
                    f"artifact ({art['filename']}) phase '{art_phase}' is not a valid phase"
                )
                continue
            art_verdict = art["verdict"]
            if art_verdict and art_verdict not in _valid_verdicts.get(art_phase, []):
                discrepancies.append(
                    f"artifact ({art['filename']}) verdict '{art_verdict}' "
                    f"is not valid for phase '{art_phase}'"
                )

        latest = artifacts[-1]
        art_phase = latest["phase"]
        if (check_phase_agreement and ry_phase and art_phase and ry_phase not in ("done",)
                and ry_phase != art_phase):
            discrepancies.append(
                f"run.yaml current_phase ('{ry_phase}') disagrees with latest artifact "
                f"phase ('{art_phase}', {latest['filename']})"
            )
        seen_steps = [a["step"] for a in artifacts]
        if not check_step_sequence:
            return {"task_id": task_id, "ok": not discrepancies, "discrepancies": discrepancies}
        duplicate_steps = sorted({step for step in seen_steps if seen_steps.count(step) > 1})
        for step in duplicate_steps:
            filenames = [a["filename"] for a in artifacts if a["step"] == step]
            discrepancies.append(
                f"step number {step} is used by more than one artifact: {filenames}"
            )
        unique_steps = sorted(set(seen_steps))
        expected_steps = list(range(unique_steps[0], unique_steps[0] + len(unique_steps)))
        if unique_steps != expected_steps:
            discrepancies.append(f"artifact step numbers are not a contiguous ascending sequence: {unique_steps}")
    elif status == "in_progress":
        discrepancies.append("status is in_progress but no artifacts exist")

    return {"task_id": task_id, "ok": not discrepancies, "discrepancies": discrepancies}


def dry_run_migration(tasks_dir: Path) -> dict:
    """Scan every task directory under tasks_dir and reconcile it. Purely
    read-only: no database writes, no file writes. Returns a summary fit
    for a human to review before any real import happens.

    A directory with neither run.yaml nor numbered agent artifacts is a
    brief-only task that never started, and is skipped. A directory with
    agent artifacts but no run.yaml is real historical work: it is always
    reported, and always flagged, so it can never be silently left out of
    a migration or imported as clean.
    """
    if not tasks_dir.exists():
        return {"total": 0, "with_run_yaml": 0, "without_run_yaml": 0,
                "clean": 0, "flagged": 0, "results": []}

    results = []
    with_run_yaml = 0
    without_run_yaml = 0
    for entry in sorted(tasks_dir.iterdir()):
        if not entry.is_dir():
            continue
        file_state = read_file_state(entry)
        has_run_yaml = (entry / "run.yaml").exists()
        if has_run_yaml:
            with_run_yaml += 1
            results.append(reconcile_task(entry.name, file_state))
            continue
        if not file_state["artifacts"]:
            continue  # brief-only task, never started -- nothing to migrate
        without_run_yaml += 1
        result = reconcile_task(entry.name, file_state)
        artifact_steps = [a["step"] for a in file_state["artifacts"]]
        result["discrepancies"].insert(
            0,
            f"no run.yaml, but {len(artifact_steps)} agent artifact(s) exist "
            f"(steps {artifact_steps}) -- real work with no task state to import",
        )
        result["ok"] = False
        results.append(result)

    flagged = [r for r in results if not r["ok"]]
    return {
        "total": len(results),
        "with_run_yaml": with_run_yaml,
        "without_run_yaml": without_run_yaml,
        "clean": len(results) - len(flagged),
        "flagged": len(flagged),
        "results": results,
    }


_DECISION_KEYS = frozenset({"task", "status", "current_phase", "exclude", "note",
                            "step_overrides", "missing_steps", "note_files"})
_STEP_OVERRIDE_KEYS = frozenset({"step", "expected", "canonical", "note"})


# Python: Path (from pathlib) is an object for file paths: path.read_text(),
#   path.exists(), path / 'name' to join. Safer than gluing strings.
def load_decisions(path: Path) -> dict[str, MigrationDecision]:
    """Read a YAML decisions file into MigrationDecisions, keyed by task id.

    Strict on purpose: an unknown key, a wrong type, a duplicate task, or a
    missing note is an error naming the field, never coerced or ignored. A
    typo in a decisions file must not quietly become "no decision", and a
    quoted "false" must not become an exclusion.

    Format:
        decisions:
          - task: some-task
            status: completed
            current_phase: done
            note: why this mapping is right
            missing_steps: [4]
            note_files: [05-prod-promotion-plan.md]
            step_overrides:
              - {step: 1, expected: approved, canonical: ready-for-builder, note: why}
    """
    if _yaml is None:
        raise RuntimeError("PyYAML is required to read a decisions file")
    try:
        document = _yaml.safe_load(path.read_text())
    except _yaml.YAMLError as error:
        raise ValueError(f"{path}: not valid YAML ({error.__class__.__name__})") from error
    if not isinstance(document, dict) or not isinstance(document.get("decisions"), list):
        raise ValueError(f"{path}: expected a mapping with a top-level 'decisions:' list")

    decisions: dict[str, MigrationDecision] = {}
    # Python: enumerate() yields (index, item) pairs; start=1 makes the count
    #   human-friendly for error messages ('decision #3').
    for position, entry in enumerate(document["decisions"], start=1):
        where = f"{path}: decision #{position}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping, got {type(entry).__name__}")
        unknown_keys = set(entry) - _DECISION_KEYS
        if unknown_keys:
            raise ValueError(f"{where}: unknown keys {sorted(unknown_keys)}")
        task_id = _required_text(entry, "task", where)
        where = f"{path}: decision for '{task_id}'"
        if task_id in decisions:
            raise ValueError(f"{path}: more than one decision for '{task_id}'")

        decisions[task_id] = MigrationDecision(
            task_id=task_id,
            status=_optional_text(entry, "status", where),
            current_phase=_optional_text(entry, "current_phase", where),
            exclude=_optional_bool(entry, "exclude", where),
            note=_required_text(entry, "note", where),
            step_overrides=tuple(_load_step_override(override, where)
                                 for override in _optional_list(entry, "step_overrides", where)),
            missing_steps=tuple(_list_of(entry, "missing_steps", int, where)),
            note_files=tuple(_list_of(entry, "note_files", str, where)),
        )
    return decisions


def _load_step_override(override, where: str) -> StepVerdictOverride:
    if not isinstance(override, dict):
        raise ValueError(f"{where}: each step override must be a mapping")
    unknown_keys = set(override) - _STEP_OVERRIDE_KEYS
    if unknown_keys:
        raise ValueError(f"{where}: unknown keys {sorted(unknown_keys)} in a step override")
    step = override.get("step")
    if not isinstance(step, int) or isinstance(step, bool):
        raise ValueError(f"{where}: step override 'step' must be an integer")
    return StepVerdictOverride(
        step=step,
        expected_verdict=_required_text(override, "expected", where),
        canonical_verdict=_required_text(override, "canonical", where),
        note=_required_text(override, "note", where),
    )


def _required_text(mapping: dict, key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return value


def _optional_text(mapping: dict, key: str, where: str) -> str | None:
    if mapping.get(key) is None:
        return None
    return _required_text(mapping, key, where)


def _optional_bool(mapping: dict, key: str, where: str) -> bool:
    value = mapping.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: '{key}' must be true or false (unquoted), got {value!r}")
    return value


def _optional_list(mapping: dict, key: str, where: str) -> list:
    value = mapping.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where}: '{key}' must be a list, got {type(value).__name__}")
    return value


# Python: isinstance(x, SomeType) checks a value's type at runtime. bool is a
#   subclass of int in Python, so True would pass as an int; that's why
#   the check below rejects bools explicitly.
def _list_of(mapping: dict, key: str, item_type: type, where: str) -> list:
    items = _optional_list(mapping, key, where)
    for item in items:
        if not isinstance(item, item_type) or isinstance(item, bool):
            raise ValueError(f"{where}: every item in '{key}' must be a {item_type.__name__}, "
                             f"got {item!r}")
    return items


def rehearse_migration(tasks_dir: Path, decisions: dict[str, MigrationDecision]) -> dict:
    """Prove what a real import would do, without touching the real database
    or any file: every task is imported into a throwaway in-memory SQLite
    database, with its decision if it has one.

    Every scanned task lands in exactly one bucket:
      clean      -- no discrepancies, no decision needed
      resolved   -- had discrepancies (or a decision) and imports cleanly with it
      excluded   -- a decision deliberately leaves it out
      unresolved -- would fail to import; the reason says why
    Decisions naming a task the scan never saw are reported, not ignored.
    """
    summary = dry_run_migration(tasks_dir)
    buckets: dict[str, list] = {"clean": [], "resolved": [], "excluded": [],
                                "unresolved": [], "unknown_decisions": []}

    # Python: ':memory:' is a database that lives only in RAM and vanishes when
    #   closed, which makes it a perfect throwaway rehearsal space.
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    scratch.execute("PRAGMA foreign_keys = ON")
    init_schema(scratch)

    scanned_task_ids = set()
    try:
        # Python: try/finally (around this call): the finally block runs even if an
        #   exception escapes, so the scratch connection is always closed.
        _rehearse_each_task(summary, decisions, tasks_dir, scratch, buckets, scanned_task_ids)
    finally:
        scratch.close()
    buckets["unknown_decisions"] = sorted(set(decisions) - scanned_task_ids)
    return {"scanned": summary["total"], **buckets}


def _rehearse_each_task(summary: dict, decisions: dict, tasks_dir: Path,
                        scratch: sqlite3.Connection, buckets: dict, scanned_task_ids: set) -> None:
    for result in summary["results"]:
        task_id = result["task_id"]
        scanned_task_ids.add(task_id)
        decision = decisions.get(task_id)
        if decision is not None and decision.exclude:
            buckets["excluded"].append(task_id)
            continue
        if decision is None and not result["ok"]:
            buckets["unresolved"].append((task_id, "flagged and no decision: "
                                          + "; ".join(result["discrepancies"])))
            continue
        try:
            import_task(scratch, task_id, tasks_dir / task_id, decision=decision)
        except (ValueError, sqlite3.Error) as error:
            buckets["unresolved"].append((task_id, str(error)))
            continue
        buckets["resolved" if decision is not None else "clean"].append(task_id)


def import_task(conn: sqlite3.Connection, task_id: str, task_dir: Path,
                 *, decision: "MigrationDecision | None" = None, force: bool = False) -> dict:
    """Actually import one task's file state into SQLite. Non-destructive:
    source files are never modified or deleted.

    Canonical-state guarantee: the status and current_phase actually
    written are ALWAYS validated against the same VALID_STATUSES/
    VALID_PHASES this module enforces everywhere else, with no exception.
    A legacy value that isn't already canonical can only become canonical
    by way of an explicit, audited `decision` (see MigrationDecision) --
    never by force=True alone. force=True governs only proceeding past
    OTHER discrepancies reconcile_task() may find (step-sequence gaps,
    etc.) that a `decision` doesn't address; it can never cause a
    non-canonical status/phase/verdict to be written.

    decision.exclude=True raises MigrationExcluded unconditionally --
    exclusion is absolute, not a discrepancy to force past.

    Refuses (raises InvalidTransition) if reconcile_task() found
    discrepancies that neither `decision` nor `force` resolve.

    Migration machinery: `relay recover` calls it, and tests exercise it
    against fixture corpora.
    """
    if decision is not None and decision.task_id != task_id:
        raise ValueError(
            f"decision.task_id ('{decision.task_id}') does not match task_id ('{task_id}')"
        )
    if decision is not None and decision.exclude:
        raise MigrationExcluded(f"'{task_id}' is explicitly excluded from migration: {decision.note}")

    if task_exists(conn, task_id):
        raise ValueError(f"task '{task_id}' already exists in the database")

    file_state = read_file_state(task_dir)
    result = reconcile_task(task_id, file_state)
    if not result["ok"] and not force:
        if decision is None:
            raise InvalidTransition(
                f"refusing to import '{task_id}': discrepancies found: {result['discrepancies']}"
            )
        unaddressed = _discrepancies_left_after_decision(task_id, file_state, decision)
        if unaddressed:
            raise InvalidTransition(
                f"refusing to import '{task_id}': the decision does not address: {unaddressed}"
            )

    run_yaml = file_state["run_yaml"]
    has_run_yaml = (task_dir / "run.yaml").exists()
    if not has_run_yaml and (decision is None or not decision.status or not decision.current_phase):
        raise InvalidTransition(
            f"cannot import '{task_id}': it has no run.yaml, so its status and current_phase "
            f"must come from an explicit migration decision"
        )

    step_artifacts, note_artifacts = _split_note_files(task_id, file_state["artifacts"], decision)
    _require_importable_steps(task_id, step_artifacts, decision, run_yaml=file_state["run_yaml"],
                              force=force)
    _require_overrides_apply(task_id, step_artifacts, decision)
    task_created, task_updated = _task_timestamps(task_id, run_yaml, file_state["artifacts"])

    ry_status = run_yaml.get("status", "in_progress")
    ry_phase = run_yaml.get("current_phase")

    final_status = decision.status if (decision is not None and decision.status) else ry_status
    final_phase = decision.current_phase if (decision is not None and decision.current_phase) else ry_phase

    # Whatever is about to be written (the legacy value or a decision's
    # replacement) must already be canonical. There is no fallback
    # substitution: an uncorrected non-canonical value fails at import
    # time, with a message naming exactly what's missing, instead of
    # writing bad data that only crashes a later `relay next` (a status of
    # 'complete', say).
    if final_status not in _valid_statuses:
        raise InvalidTransition(
            f"cannot import '{task_id}': status '{final_status}' is not canonical "
            f"(legacy value '{ry_status}' has no migration decision mapping it to one of "
            f"{_valid_statuses})"
        )
    if final_phase not in _valid_phases and final_phase != "done":
        raise InvalidTransition(
            f"cannot import '{task_id}': current_phase '{final_phase}' is not canonical "
            f"(legacy value '{ry_phase}' has no migration decision mapping it to one of "
            f"{_valid_phases + ['done']})"
        )

    # Step verdicts get the same treatment, as record_step_and_save() (the
    # live path) gives them. A task with an invalid step verdict and no decision
    # addressing it fails here, before anything is written, rather than
    # corrupting canonical state -- and a decision only ever applies when
    # its expected_verdict matches exactly what's actually on disk, so a
    # decision that's gone stale (the file changed since it was authored)
    # fails loudly instead of silently mis-applying.
    final_verdicts: dict[int, str | None] = {}
    step_overrides_applied: list[tuple[int, str, str]] = []  # (step, legacy, canonical)
    for art in step_artifacts:
        v = art.get("verdict")
        p = art["phase"]
        step = art["step"]
        if not v or v in _valid_verdicts.get(p, []):
            final_verdicts[step] = v
            continue

        override = decision.step_override_for(step) if decision is not None else None
        if override is None:
            raise InvalidTransition(
                f"cannot import '{task_id}': step {step} ({art['filename']}) verdict "
                f"'{v}' is not valid for phase '{p}'"
            )
        if override.expected_verdict != v:
            raise InvalidTransition(
                f"cannot import '{task_id}': step {step} override expected verdict "
                f"'{override.expected_verdict}' but found '{v}' in {art['filename']} -- "
                f"refusing to apply a decision against data it doesn't match"
            )
        if override.canonical_verdict not in _valid_verdicts.get(p, []):
            raise InvalidTransition(
                f"cannot import '{task_id}': step {step} override's canonical_verdict "
                f"'{override.canonical_verdict}' is not valid for phase '{p}'"
            )
        final_verdicts[step] = override.canonical_verdict
        step_overrides_applied.append((step, v, override.canonical_verdict))

    # Per-step timestamps: prefer run.yaml's own history entries (they carry
    # a real, fine-grained timestamp per phase transition) over the task's
    # single overall `updated` value, which the pre-fix code stamped onto
    # every step, destroying chronology. Where a step's real timestamp is
    # genuinely unrecoverable (no matching history entry, no artifact `date`
    # frontmatter either), created_at is left NULL -- not fabricated.
    step_timestamps: dict[int, str] = {}
    for entry in run_yaml.get("history", []) or []:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        ts = _timestamp_text(entry.get("timestamp"))
        if step is not None and ts:
            step_timestamps[step] = ts  # last entry for a duplicated step wins

    # Task-row creation and every other write this import performs (the
    # UPDATE, steps, migration_override events, historical events,
    # nonstandard-field event) happen in ONE transaction via
    # _insert_task_row(), not by calling the self-committing create_task().
    # Committing the task row on its own would let a failure further down
    # leave an orphaned tasks row with no steps or events, and
    # task_exists() would then refuse every retry. With a single
    # transaction any failure rolls back the task-row insert too, so a
    # retry of import_task() after a failure needs no manual cleanup.
    with conn:
        _insert_task_row(conn, task_id, repo=run_yaml.get("repo"),
                          deploy_intent=bool(run_yaml.get("deploy_intent", False)),
                          current_phase=final_phase)
        conn.execute(
            """UPDATE tasks SET created=?, updated=?, status=?, current_phase=?,
                   worktree_path=?, branch=?, base=?, next_step=? WHERE task_id=?""",
            (task_created, task_updated,
             final_status, final_phase,
             run_yaml.get("worktree_path"), run_yaml.get("branch"), run_yaml.get("base"),
             run_yaml.get("next_step") or _next_step_from_trail(step_artifacts, decision), task_id),
        )
        for art in step_artifacts:
            created_at = step_timestamps.get(art["step"])
            if created_at is None and art.get("date"):
                created_at = f"{art['date']}T00:00:00Z"  # day-only, real, lower fidelity
            conn.execute(
                """INSERT INTO steps
                       (task_id, step, phase, verdict, artifact_filename, content_hash, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (task_id, art["step"], art["phase"], final_verdicts.get(art["step"]),
                 art["filename"], art["content_hash"], created_at),
            )

        if not has_run_yaml:
            conn.execute(
                "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
                (task_id, EVENT_HISTORICAL_NOTE,
                 json.dumps({"source": "no run.yaml on disk; status and current_phase supplied "
                                       "by migration decision", "note": decision.note}),
                 _now()),
            )
        for note_artifact in note_artifacts:
            conn.execute(
                "INSERT INTO events (task_id, event_type, step, payload, timestamp) VALUES (?,?,?,?,?)",
                (task_id, EVENT_HISTORICAL_NOTE, note_artifact["step"],
                 json.dumps({"note_file": note_artifact["filename"],
                             "content_hash": note_artifact["content_hash"],
                             "note": decision.note}),
                 _now()),
            )
        for missing_step in (decision.missing_steps if decision is not None else ()):
            conn.execute(
                "INSERT INTO events (task_id, event_type, step, payload, timestamp) VALUES (?,?,?,?,?)",
                (task_id, EVENT_STEP_NEVER_DELIVERED, missing_step,
                 json.dumps({"note": decision.note}), _now()),
            )

        for step, legacy_verdict, canonical_verdict in step_overrides_applied:
            override = decision.step_override_for(step)
            conn.execute(
                "INSERT INTO events (task_id, event_type, step, payload, timestamp) VALUES (?,?,?,?,?)",
                (task_id, EVENT_MIGRATION_OVERRIDE, step,
                 json.dumps({"field": "verdict", "legacy": legacy_verdict,
                             "canonical": canonical_verdict, "note": override.note}),
                 _now()),
            )

        # Audit trail: record exactly what was overridden and why, distinct
        # from ordinary state_saved/task_created events. Never fires when
        # decision is None or doesn't touch a given field.
        if decision is not None:
            if decision.status:
                conn.execute(
                    "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
                    (task_id, EVENT_MIGRATION_OVERRIDE,
                     json.dumps({"field": "status", "legacy": ry_status,
                                 "canonical": decision.status, "note": decision.note}),
                     _now()),
                )
            if decision.current_phase:
                conn.execute(
                    "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
                    (task_id, EVENT_MIGRATION_OVERRIDE,
                     json.dumps({"field": "current_phase", "legacy": ry_phase,
                                 "canonical": decision.current_phase, "note": decision.note}),
                     _now()),
                )

        _import_historical_events(conn, task_id, run_yaml, step_artifacts)
        _import_nonstandard_fields(conn, task_id, run_yaml)

    return result


# Python: dict(x) makes a shallow copy, so changing the copy below doesn't
#   change the caller's data.
def _discrepancies_left_after_decision(task_id: str, file_state: dict,
                                       decision: "MigrationDecision") -> list[str]:
    """Reconcile the task as it would look with the decision applied, so a
    decision only excuses the problems it actually addresses.

    Step gaps and duplicates are checked separately by
    _require_importable_steps(). A run.yaml phase that disagrees with the
    latest artifact counts as addressed only when the decision sets
    current_phase explicitly, which import_task() records as an audited
    migration_override event."""
    adjusted_run_yaml = dict(file_state["run_yaml"])
    if decision.status:
        adjusted_run_yaml["status"] = decision.status
    if decision.current_phase:
        adjusted_run_yaml["current_phase"] = decision.current_phase

    adjusted_artifacts = []
    for artifact in file_state["artifacts"]:
        if artifact["filename"] in decision.note_files:
            continue
        adjusted = dict(artifact)
        override = decision.step_override_for(artifact["step"])
        if override is not None:
            if override.expected_verdict != artifact["verdict"]:
                raise InvalidTransition(
                    f"cannot import '{task_id}': step {artifact['step']} override expected verdict "
                    f"'{override.expected_verdict}' but found '{artifact['verdict']}' in "
                    f"{artifact['filename']} -- refusing to apply a decision against data it "
                    f"doesn't match"
                )
            adjusted["verdict"] = override.canonical_verdict
        adjusted_artifacts.append(adjusted)

    return reconcile_task(
        task_id, {"run_yaml": adjusted_run_yaml, "artifacts": adjusted_artifacts},
        check_step_sequence=False,
        check_phase_agreement=decision.current_phase is None,
    )["discrepancies"]


def _split_note_files(task_id: str, artifacts: list[dict],
                      decision: "MigrationDecision | None") -> tuple[list[dict], list[dict]]:
    """Separate the artifacts a decision names as notes from the real
    pipeline steps. A named file that isn't on disk fails loudly: the
    decision was written against different data."""
    # Python: A 'conditional expression': A if condition else B, in one line.
    #   Sets give fast membership checks and set math like a - b below.
    note_filenames = set(decision.note_files) if decision is not None else set()
    present = {artifact["filename"] for artifact in artifacts}
    unknown = sorted(note_filenames - present)
    if unknown:
        raise InvalidTransition(
            f"cannot import '{task_id}': note_files {unknown} are not agent artifacts on disk"
        )
    step_artifacts = [a for a in artifacts if a["filename"] not in note_filenames]
    note_artifacts = [a for a in artifacts if a["filename"] in note_filenames]
    return step_artifacts, note_artifacts


def _require_importable_steps(task_id: str, step_artifacts: list[dict],
                              decision: "MigrationDecision | None", *, run_yaml: dict,
                              force: bool) -> None:
    """Refuse anything that would make import invent or silently drop
    history: a step with no phase, two files sharing a step number, a
    declared-missing step that actually exists or has no evidence, or an
    unexplained gap. force skips only the gap check."""
    for artifact in step_artifacts:
        if artifact["phase"] is None:
            raise InvalidTransition(
                f"cannot import '{task_id}': {artifact['filename']} has no frontmatter phase; "
                f"list it in the decision's note_files to import it as a note"
            )

    step_numbers = [a["step"] for a in step_artifacts]
    # Python: {... for ... if ...} is a set comprehension: build a set from a loop
    #   in one expression. Using a set drops repeats automatically.
    duplicates = sorted({n for n in step_numbers if step_numbers.count(n) > 1})
    if duplicates:
        raise InvalidTransition(
            f"cannot import '{task_id}': step numbers {duplicates} are used by more than one "
            f"artifact; list the extra files in the decision's note_files"
        )

    missing_steps = set(decision.missing_steps) if decision is not None else set()
    contradicted = sorted(missing_steps & set(step_numbers))
    if contradicted:
        raise InvalidTransition(
            f"cannot import '{task_id}': missing_steps {contradicted} actually exist on disk"
        )
    if missing_steps and step_numbers:
        before_first = sorted(n for n in missing_steps if n < min(step_numbers))
        if before_first:
            raise InvalidTransition(
                f"cannot import '{task_id}': missing_steps {before_first} come before the "
                f"first artifact (step {min(step_numbers)})"
            )
        history_steps = {entry.get("step") for entry in run_yaml.get("history", []) or []
                         if isinstance(entry, dict)}
        unevidenced = sorted(n for n in missing_steps
                             if n > max(step_numbers) and n not in history_steps)
        if unevidenced:
            raise InvalidTransition(
                f"cannot import '{task_id}': missing_steps {unevidenced} come after the last "
                f"artifact and run.yaml history has no record of them"
            )

    if force or not step_numbers:
        return
    accounted_for = sorted(set(step_numbers) | missing_steps)
    expected = list(range(accounted_for[0], accounted_for[-1] + 1))
    if accounted_for != expected:
        gaps = sorted(set(expected) - set(accounted_for))
        raise InvalidTransition(
            f"cannot import '{task_id}': steps {gaps} are missing; declare them in the "
            f"decision's missing_steps if they were never delivered"
        )


# Python: max(..., default=0) returns 0 for an empty list instead of raising.
def _next_step_from_trail(step_artifacts: list[dict], decision: "MigrationDecision | None") -> int:
    """One past the highest step on disk or declared missing."""
    numbers = [artifact["step"] for artifact in step_artifacts]
    numbers += list(decision.missing_steps) if decision is not None else []
    return max(numbers, default=0) + 1


def _require_overrides_apply(task_id: str, step_artifacts: list[dict],
                             decision: "MigrationDecision | None") -> None:
    """Every step override must hit a real step whose current verdict is the
    one it expects and is not already canonical. A stale or pointless
    override means the decision was written against different data."""
    if decision is None:
        return
    artifacts_by_step = {artifact["step"]: artifact for artifact in step_artifacts}
    for override in decision.step_overrides:
        artifact = artifacts_by_step.get(override.step)
        if artifact is None:
            raise InvalidTransition(
                f"cannot import '{task_id}': step override for step {override.step} "
                f"matches no step on disk"
            )
        if artifact["verdict"] != override.expected_verdict:
            raise InvalidTransition(
                f"cannot import '{task_id}': step {override.step} override expected verdict "
                f"'{override.expected_verdict}' but found '{artifact['verdict']}' in "
                f"{artifact['filename']} -- refusing to apply a decision against data it "
                f"doesn't match"
            )
        if artifact["verdict"] in _valid_verdicts.get(artifact["phase"], []):
            raise InvalidTransition(
                f"cannot import '{task_id}': step {override.step} verdict "
                f"'{artifact['verdict']}' is already canonical; the override is stale"
            )


def _task_timestamps(task_id: str, run_yaml: dict, artifacts: list[dict]) -> tuple[str, str]:
    """created/updated for the task row: run.yaml's own values when present,
    otherwise the earliest and latest real artifact dates. Never invented:
    no dates anywhere means the import is refused."""
    if run_yaml.get("created") and run_yaml.get("updated"):
        return _timestamp_text(run_yaml["created"]), _timestamp_text(run_yaml["updated"])
    dates = sorted(str(a["date"]) for a in artifacts if a.get("date"))
    if not dates:
        raise InvalidTransition(
            f"cannot import '{task_id}': no run.yaml timestamps and no artifact dates to "
            f"derive created/updated from"
        )
    return (_timestamp_text(run_yaml.get("created")) or f"{dates[0]}T00:00:00Z",
            _timestamp_text(run_yaml.get("updated")) or f"{dates[-1]}T00:00:00Z")


# Python: 'str | None' in a hint means 'a string, or None'.
def _timestamp_text(value) -> str | None:
    """YAML turns an unquoted timestamp into a datetime (or a date); store
    it in the same ISO-8601 'Z' form _now() writes, so every timestamp
    compares and sorts the same way."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    return str(value)


# Python: json.dumps(..., default=fn) calls fn for any value it can't serialize
#   on its own (like dates). Raising TypeError there keeps the normal error.
def _legacy_json_value(value):
    """json.dumps fallback for values YAML parsed out of legacy files:
    dates and datetimes become timestamp text instead of crashing."""
    if isinstance(value, (datetime, date)):
        return _timestamp_text(value)
    raise TypeError(f"cannot store {type(value).__name__} from a legacy file as JSON")


# run.yaml keys import_task()/create_task() actually read into canonical
# columns. Anything else is non-standard, task-specific bookkeeping a human
# or tool added directly to the file (calibration results, scope notes,
# parked-reason text, base-branch provenance, etc.) -- real information,
# never canonical Relay state.
_CANONICAL_RUN_YAML_KEYS = frozenset({
    "task_id", "created", "updated", "status", "current_phase", "next_step",
    "schema_version", "repo", "worktree_path", "branch", "base",
    "deploy_intent", "retries", "history",
})


def _import_nonstandard_fields(conn: sqlite3.Connection, task_id: str, run_yaml: dict) -> None:
    """Preserve any run.yaml top-level fields outside the canonical schema
    (e.g. a freeform `results:` block of notes) as one historical-metadata
    event. Never touches tasks/steps.
    No new schema field -- events.payload already exists for exactly this.
    """
    extra = {k: v for k, v in run_yaml.items() if k not in _CANONICAL_RUN_YAML_KEYS}
    if not extra:
        return
    conn.execute(
        "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
        (task_id, EVENT_HISTORICAL_NOTE, json.dumps(extra, default=_legacy_json_value), _now()),
    )


def _import_historical_events(conn: sqlite3.Connection, task_id: str, run_yaml: dict,
                               artifacts: list[dict]) -> None:
    """Preserve run.yaml's history[] entries as pure historical record --
    events rows, never canonical tasks/steps columns.

    This is specifically for entries that use non-canonical phase
    vocabulary Relay itself never had (human_gate, builder_fix,
    builder_commit, builder_harden, committed, closeout -- observed in the
    real corpus) or that simply have no corresponding artifact file. It
    also captures real data those entries carry that canonical state has
    nowhere else to hold, most notably git commit hashes on `committed`/
    `builder_commit` entries. Relay itself never reads this table's
    `payload` to make a routing decision -- it exists purely so "what
    happened to this task" stays a query instead of a forensic file read,
    per the persistence-v2 brief's own Problem Statement. Historical
    vocabulary never touches VALID_PHASES, never becomes a `steps.phase`
    or `tasks.current_phase` value.
    """
    imported_steps = {a["step"] for a in artifacts}
    for entry in run_yaml.get("history", []) or []:
        if not isinstance(entry, dict):
            continue
        step = entry.get("step")
        phase = entry.get("phase")
        # An entry backed by a real artifact is already faithfully
        # represented in `steps` -- only record the ones that aren't,
        # so this doesn't duplicate the normal step trail.
        if step is not None and step in imported_steps and phase in _valid_phases:
            continue
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_HISTORICAL_NOTE, phase, step,
             json.dumps(entry, default=_legacy_json_value),
             _timestamp_text(entry.get("timestamp")) or _now()),
        )
