"""
Relay SQLite persistence layer.

Canonical orchestration state lives here once `persistence_backend: sqlite`
is set in config.yaml. Markdown/YAML (run.yaml, artifact frontmatter) become
generated projections of this database, never written to directly by the
sqlite-backed code paths in Relay's other modules.

Design:

  tasks  -- one row per task, the current-state snapshot. Never hand-edited;
            written by create_task() and update_task() inside a
            transaction.
  steps  -- one row per numbered agent artifact (phase, step, verdict,
            filename, content hash). Routing reads the latest step from
            here, not from run.yaml or a report's frontmatter
            (run_state.find_latest_artifact).
  events -- insert-only audit log. Anything steps/tasks can't capture on
            its own (retries, human overrides/approvals, worktree lifecycle,
            status and phase changes) gets an event row here. This is deliberately
            not full event sourcing: tasks/steps are still the fast-path
            read model; events exist so "what happened to this task and
            when" is a query, not a forensic file read.

Each thing that happens is recorded by its own call at the moment it
happens: record_dispatch() when a step starts (with the report file the
agent was told to write), record_verdict() when its verdict is read,
record_retry() when an automatic round is counted, update_task() when the
task's own fields change. Nothing is worked out afterwards by comparing a
state dict with the ledger. transaction() lets a command make several of
these calls and commit them together.

The ledger records its own schema version in SQLite's user_version and
upgrades itself, after a backup, the first time it is opened for writing
by a newer Relay (see "Ledger version" below).

The one-time move from task files lives in migrate_legacy.py, which uses
this module; this module knows nothing of it.

This module imports only bounded_yaml from Relay, which is standalone. The
caller (settings.py) calls configure() before opening the ledger, with its VALID_PHASES /
VALID_VERDICTS / VALID_STATUSES so there is exactly one place those enums
are defined -- this module enforces them, it does not redefine them.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - settings.py already fails fast on this
    _yaml = None

import bounded_yaml


# The per-task schema_version column and run.yaml key. It is not the
# ledger's own version (LEDGER_VERSION, below), and it stays 1.
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

# Event types. Not an enforced enum (events is intentionally a loose audit
# log), just documentation of the vocabulary. Every constant stays here, so
# readers and tests name them all as db.EVENT_*.

# -- Live: written by Relay.
EVENT_TASK_CREATED = "task_created"
# A save changed the task's status or current phase (update_task). A save
# that changes neither writes none; older ledgers hold one for every save.
EVENT_STATE_SAVED = "state_saved"
EVENT_RETRY = "retry"
EVENT_APPROVAL = "approval"
EVENT_WORKTREE_CREATED = "worktree_created"
EVENT_WORKTREE_FINALIZED = "worktree_finalized"
# `relay next` read a step's verdict from its report (sync_step_verdict_from_file).
EVENT_VERDICT_RECORDED = "verdict_recorded"
EVENT_DEPLOY_DECIDED = "deploy_decided"
EVENT_SENT_BACK = "sent_back"
EVENT_TEST_RUN = "test_run"
EVENT_TEST_COMMAND_ACCEPTED = "test_command_accepted"
EVENT_SLEPT_DURING_STEP = "slept_during_step"
EVENT_OWNER_NOTE = "owner_note"
# A role started under --auto ran past its time limit and was stopped (agents.py).
EVENT_TIME_LIMIT_STOP = "time_limit_stop"
# What `relay start` read of the task's size, risk and deploy (sizing.py),
# and again for every later change: a raise from a Reviewer's or Arbiter's
# report, or the owner's `relay resize` (record_sizing_change). Every reader
# takes the latest, so a Relay from before changes existed reads it too.
EVENT_SIZED = "sized"
# The sizing hint worked out after a Builder step (risk_hint.py). Read by
# risk_record: the next judging role's prompt, status, the card and the JSON API.
EVENT_RISK_HINT = "risk_hint"
# What a Reviewer's or Arbiter's report said of the sizing, where it
# answered, asked for something Relay refused, or did not answer a hint it
# had to. Read by risk_record.
EVENT_SIZING_JUDGED = "sizing_judged"
# The commit Relay confirmed for a task's base (base_trust.py): at `relay
# start`, at the first check of a task recorded before base commits existed,
# and at each forward move. The newest row is the task's base commit. Read
# only by base_trust; every other reader of events passes over it.
EVENT_BASE_CONFIRMED = "base_confirmed"
# The Arbiter was dispatched (commands._dispatch_next). Read by
# arbiter_record, for what a later Arbiter is handed.
EVENT_ARBITER_STARTED = "arbiter_started"
# Relay acted on an Arbiter's ruling (commands._record_the_arbiter_outcome).
# Read by arbiter_record.read_rulings and read_follow_ups: status, the card,
# the JSON read API and later prompts.
EVENT_ARBITER_RULED = "arbiter_ruled"
# Relay could not act on an Arbiter's report. Read by arbiter_record and by
# read_api.read_gate, which shows the arbiter_unreadable gate from it.
EVENT_ARBITER_UNREADABLE = "arbiter_unreadable"
# The owner accepted a step and logged its remaining findings as follow-ups
# (`relay decide ... accept-and-log`). Read by arbiter_record.read_follow_ups.
EVENT_FOLLOW_UPS_LOGGED = "follow_ups_logged"

# -- Written only by migrate_legacy.py, when it imports a task from files.
EVENT_MIGRATION_OVERRIDE = "migration_override"
EVENT_HISTORICAL_NOTE = "historical_note"
EVENT_STEP_NEVER_DELIVERED = "step_never_delivered"
MIGRATION_EVENT_TYPES = frozenset({
    EVENT_MIGRATION_OVERRIDE, EVENT_HISTORICAL_NOTE, EVENT_STEP_NEVER_DELIVERED,
})

# -- Legacy, read-only: nothing writes these; older ledgers hold them and
# every reader passes over them.
# Written by a check Relay once made on each role's report.
EVENT_REPORT_CHECK = "report_check"
# Written by a process audit Relay once ran.
EVENT_AUDIT_STARTED = "audit_started"
EVENT_AUDIT_REPORTED = "audit_reported"
LEGACY_EVENT_TYPES = frozenset({EVENT_REPORT_CHECK, EVENT_AUDIT_STARTED, EVENT_AUDIT_REPORTED})


class InvalidTransition(ValueError):
    """Raised when a phase/verdict outside the configured enums is persisted.

    This is a persistence-layer integrity guard, independent of and in
    addition to routing.py's own determine_next()/ROUTING logic -- it does not
    replace that logic, it backstops it at the point of writing.
    """


class DatabaseUnavailable(RuntimeError):
    """Base class: the canonical sqlite database cannot be used as-is.

    Raised by connect_existing() -- the fail-closed entry point real
    Relay commands use once persistence_backend is sqlite -- for a missing,
    corrupt or unknown-version ledger, or one whose upgrade failed.
    connect() (used for deliberate creation, and throughout the test suite
    for low-level plumbing) happily creates a fresh database, exactly as it
    always has, and raises this only for an existing ledger whose version
    it cannot take or whose upgrade failed. The distinction is the point:
    ordinary command execution must never silently fabricate or repair
    canonical state, only an explicit, operator-invoked action may.
    """


class DatabaseMissing(DatabaseUnavailable):
    """The expected canonical database file does not exist."""


class DatabaseCorrupt(DatabaseUnavailable):
    """The database file exists but failed an integrity check, or isn't a
    valid sqlite database at all."""


# Python: a class with two bases is caught by an `except` for either.
class UnreadableEvent(DatabaseCorrupt, ValueError):
    """An event's payload cannot be read as JSON: only a hand-edited ledger
    holds one. A command stops on it with a plain message, as on any corrupt
    ledger; it is a ValueError too, as json.loads' own error was, so a
    reader that already treats such a row as unreadable (read_api's blocked
    task, for one) still does."""


class LedgerTooNew(DatabaseUnavailable):
    """The ledger was upgraded by a newer Relay than this one. It is never
    opened, written or backed up by this Relay."""


class LedgerNeedsUpgrade(DatabaseUnavailable):
    """A read-only open found a ledger that must be upgraded before this
    Relay can read it correctly. Read-only opens never upgrade."""


class LedgerUpgradeFailed(DatabaseUnavailable):
    """Backing up or upgrading the ledger failed. Nothing was changed."""


class LedgerCannotBeOpened(DatabaseUnavailable):
    """The ledger passed its health check, but SQLite refused to open it for
    use, most often because the folder it is in cannot be written: a WAL
    ledger needs its -wal and -shm files beside it, and SQLite must create
    them when they are missing. Nothing was changed."""


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
# Ledger version
# ---------------------------------------------------------------------------
# The ledger file records its own version in SQLite's PRAGMA user_version.
# A ledger behind this Relay is upgraded the first time it is opened for
# writing: under the write lock, Relay backs the file up beside itself
# (relay.db.backup-v{from}-{UTC time}), then applies each pending Upgrade in
# order inside one transaction and stamps the new version. A ledger from a
# newer Relay is refused. Version 1 is the schema every ledger has had since
# the first one, so stamping it changes no data. A future schema change
# appends an Upgrade here.

class Upgrade(NamedTuple):
    """One step that brings a ledger up to `to_version`. `apply` runs
    inside the upgrade's transaction and must use execute(), never
    executescript() (which commits). `readable_before` is True when a ledger
    that has not had this step yet still reads correctly, so a read-only
    open may read it as it is."""
    to_version: int
    apply: Callable[[sqlite3.Connection], None]
    readable_before: bool


def _baseline(conn: sqlite3.Connection) -> None:
    """Version 1: the schema every ledger already has. Nothing to change."""


_UPGRADES: list[Upgrade] = [Upgrade(1, _baseline, readable_before=True)]
LEDGER_VERSION = _UPGRADES[-1].to_version


def latest_ledger_version() -> int:
    """The version this Relay brings a ledger up to. Read from _UPGRADES at
    call time, so a test can add a step."""
    return _UPGRADES[-1].to_version


def ledger_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _pending_upgrades(found: int, latest: int) -> list[Upgrade]:
    return [upgrade for upgrade in _UPGRADES if found < upgrade.to_version <= latest]


def _too_new(db_path: Path, found: int, latest: int) -> LedgerTooNew:
    return LedgerTooNew(
        f"The ledger at {db_path} is version {found}, but this Relay knows ledger versions "
        f"up to {latest}: it was upgraded by a newer Relay. Upgrade Relay to use it. "
        "The file was not changed."
    )


def _no_such_version(db_path: Path, found: int) -> DatabaseCorrupt:
    return DatabaseCorrupt(
        f"The ledger at {db_path} says it is version {found}, which no Relay writes. "
        "Relay will not operate against it, and has not modified it."
    )


def _refuse_unknown_version(conn: sqlite3.Connection, db_path: Path) -> None:
    """Close the connection and refuse, before anything is written, a
    ledger from a newer Relay (LedgerTooNew) or one whose version no Relay
    writes, below 0 (DatabaseCorrupt)."""
    found, latest = ledger_version(conn), latest_ledger_version()
    if found > latest:
        conn.close()
        raise _too_new(db_path, found, latest)
    if found < 0:
        conn.close()
        raise _no_such_version(db_path, found)


def _upgrade_if_behind(conn: sqlite3.Connection, db_path: Path) -> None:
    """Bring a behind ledger up to this Relay's version: take the write
    lock, read the version again (another process may have upgraded it
    while this one waited), back the file up, apply each pending step in
    order and stamp the version, all in one transaction. A current ledger
    is left alone, with no write. On any failure the transaction is rolled
    back, the connection is closed and nothing in the ledger has changed."""
    latest = latest_ledger_version()
    if ledger_version(conn) == latest:
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        found = ledger_version(conn)
        if found > latest:
            raise _too_new(db_path, found, latest)
        if found < 0:
            raise _no_such_version(db_path, found)
        if found == latest:
            conn.rollback()
            return
        backup = _write_backup(db_path, found)
        try:
            for upgrade in _pending_upgrades(found, latest):
                upgrade.apply(conn)
            conn.execute(f"PRAGMA user_version = {int(latest)}")
            conn.commit()
        except Exception as error:
            raise LedgerUpgradeFailed(
                f"Upgrading the ledger at {db_path} from version {found} to {latest} failed "
                f"({error}). Nothing was changed: it is still version {found}. The backup "
                f"taken first is at {backup}."
            ) from error
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise


def _write_backup(db_path: Path, found: int) -> Path:
    """Copy the ledger to a new file beside it, named for the version it is
    and the time, and check the copy. Called with the write lock held, so
    nothing can change the ledger meanwhile.

    The copy is written to a hidden unfinished file and only gets its
    backup name once it has passed its check, so a backup name never holds
    a partial copy, even if Relay is killed mid-copy. The unfinished file is
    written, read back and checked only through the descriptor that created
    it, never reopened by name, so nothing swapped in at the name can
    redirect the copy. Never overwrites or follows anything already at a
    backup name: a name already taken gets -2, -3 and so on. Raises
    LedgerUpgradeFailed, with the unfinished file removed, when no good
    backup could be made."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{db_path.name}.backup-v{found}-{stamp}"
    try:
        unfinished, descriptor = _new_unfinished_backup(db_path)
    except OSError as error:
        raise _backup_failed(db_path, error) from error
    try:
        _copy_ledger_into(db_path, descriptor)
        _check_backup(descriptor, found)
        return _name_backup(db_path, unfinished, descriptor, name)
    except Exception as error:
        raise _backup_failed(db_path, error) from error
    finally:
        os.close(descriptor)
        unfinished.unlink(missing_ok=True)


def _backup_failed(db_path: Path, error: Exception) -> LedgerUpgradeFailed:
    return LedgerUpgradeFailed(
        f"Relay could not back up the ledger at {db_path} before upgrading it ({error}), "
        "so it did not upgrade it. Nothing was changed."
    )


def _new_unfinished_backup(db_path: Path) -> tuple[Path, int]:
    """Create, empty, the file the copy is written to before it is checked:
    hidden, named so it is never taken for a backup, open for reading and
    writing, with the ledger's permissions (the umask can only narrow
    them). O_EXCL means nothing already at the name, a planted symlink
    included, is ever opened."""
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    for _ in range(100):
        unfinished = db_path.with_name(f".{db_path.name}.unfinished-backup-{secrets.token_hex(8)}")
        try:
            return unfinished, os.open(unfinished, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode)
        except FileExistsError:
            continue
    raise FileExistsError(f"no free name for an unfinished backup beside {db_path}")


def _copy_ledger_into(db_path: Path, descriptor: int) -> None:
    """With no unmerged WAL frames the main file is the whole ledger and is
    copied byte for byte; otherwise SQLite's backup API copies it, from a
    second connection (from the caller's it would wait on its own lock),
    into memory, and those bytes are written."""
    wal = db_path.with_name(db_path.name + "-wal")
    with os.fdopen(descriptor, "wb", closefd=False) as copy:
        if wal.exists() and wal.stat().st_size > 0:
            copy.write(_ledger_bytes_through_backup_api(db_path))
        else:
            with open(db_path, "rb") as source:
                shutil.copyfileobj(source, copy)
        copy.flush()
        os.fsync(copy.fileno())


def _ledger_bytes_through_backup_api(db_path: Path) -> bytes:
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        memory = sqlite3.connect(":memory:")
        try:
            source.backup(memory)
            return memory.serialize()
        finally:
            memory.close()
    finally:
        source.close()


def _check_backup(descriptor: int, found: int) -> None:
    """The copy, read back through its own descriptor, must be a sound
    ledger at the version it was taken from. It is checked in memory, where
    SQLite cannot open a file marked for WAL mode, so the probe's copy has
    header bytes 18 and 19 (the journal mode) set to rollback first."""
    with os.fdopen(descriptor, "rb", closefd=False) as copy:
        copy.seek(0)
        written = copy.read()
    probe = sqlite3.connect(":memory:")
    try:
        probe.deserialize(written[:18] + b"\x01\x01" + written[20:])
        integrity = probe.execute("PRAGMA integrity_check").fetchone()[0]
        version = ledger_version(probe)
    finally:
        probe.close()
    if integrity != "ok" or version != found:
        raise ValueError(f"the copy failed its check (integrity {integrity}, version {version})")


def _name_backup(db_path: Path, unfinished: Path, descriptor: int, name: str) -> Path:
    """Give the checked copy its backup name, `name` or `name-2`, `name-3`...
    when taken, as a hard link: a link never replaces an existing name, and
    with follow_symlinks=False never follows one. The unfinished name could
    have been swapped since the copy was written, so the new name must be
    the very file the descriptor wrote, or it is removed again."""
    written = os.fstat(descriptor)
    for attempt in range(1, 1000):
        target = db_path.with_name(name if attempt == 1 else f"{name}-{attempt}")
        try:
            os.link(unfinished, target, follow_symlinks=False)
        except FileExistsError:
            continue
        linked = os.lstat(target)
        if (linked.st_dev, linked.st_ino) != (written.st_dev, written.st_ino):
            target.unlink()
            raise ValueError("the copy's file was replaced before it got its backup name")
        return target
    raise FileExistsError(f"Relay found no free name for a backup ({name} and -2 to -999 "
                          "are all taken)")


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def connect(db_path: Path) -> sqlite3.Connection:
    """Open db_path, creating a fresh empty database (and its parent dir)
    if nothing is there yet. Used for deliberate creation (init_database())
    and by the test suite's low-level plumbing. Relay's real production
    connection path uses connect_existing() instead -- see its docstring
    for why the two must not be merged.

    A fresh database is stamped with this Relay's ledger version and
    needs no backup. An existing one is refused if it is newer than this
    Relay, and upgraded (after a backup) if it is behind."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
                         ).fetchone() is None
    if not fresh:
        _refuse_unknown_version(conn, db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    if fresh:
        init_schema(conn)
        conn.execute(f"PRAGMA user_version = {int(latest_ledger_version())}")
        return conn
    _upgrade_if_behind(conn, db_path)
    init_schema(conn)
    return conn


# Pauses before each new try of a failed integrity probe, 0.75 seconds in
# all. While another Relay process checkpoints its writes into the main
# file, the probe can read a page mid-copy; on a ledger Relay's size a
# checkpoint takes milliseconds, so a later try reads it whole. A file that
# is really corrupt fails every try and is refused as before.
_PROBE_RETRY_PAUSES = (0.05, 0.1, 0.2, 0.4)


def _probe_once(db_path: Path) -> tuple[str | None, sqlite3.Error | None]:
    """PRAGMA integrity_check on the main file alone, as it is on disk:
    its result, or the sqlite3.Error that stopped it.

    The connection is read-only and immutable. A plain read-write open of
    a database with a stale/unmerged -wal or -journal file (left behind by
    a crash) triggers SQLite's automatic recovery replay as a side effect
    of opening it -- which writes to the main file. That is the opposite
    of what a corruption check should do to a file that might need to be
    preserved untouched for inspection or backup. immutable=1 tells SQLite
    the file will not change and skips that recovery machinery entirely,
    so a corrupt/unhealthy database is genuinely never mutated by this
    check. The price is that it takes no lock and ignores the WAL, so it
    can meet a page another process is writing."""
    try:
        probe = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=5)
        try:
            return probe.execute("PRAGMA integrity_check").fetchone()[0], None
        finally:
            probe.close()
    except sqlite3.Error as error:
        return None, error


def _probe_integrity(db_path: Path) -> tuple[str | None, sqlite3.Error | None]:
    """_probe_once until it passes or every pause in _PROBE_RETRY_PAUSES
    has been waited out, and the last probe's outcome. Every try is the
    same immutable probe, so retrying changes nothing on disk."""
    for pause in _PROBE_RETRY_PAUSES:
        integrity, error = _probe_once(db_path)
        if integrity == "ok":
            return integrity, error
        time.sleep(pause)
    return _probe_once(db_path)


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
    itself refuses to write through. It never upgrades or backs up: a
    ledger it cannot read correctly as it is raises LedgerNeedsUpgrade.

    Either way a ledger from a newer Relay raises LedgerTooNew, untouched.
    A read-write open of a ledger that is behind backs it up and upgrades it
    first (see "Ledger version").
    """
    if not db_path.exists():
        raise DatabaseMissing(
            f"No canonical database found at {db_path}. Relay will not silently "
            "create a new one -- persistence_backend=sqlite means this file is "
            "the single source of truth for orchestration state. If this is "
            "genuinely a first-time setup, run 'relay recover --init'. If a "
            "database used to be here, restore it from backup before continuing."
        )

    # Check the file FIRST, before ever opening a normal read-write
    # connection; _probe_once says how the check leaves the file untouched.
    integrity, error = _probe_integrity(db_path)
    if error is not None:
        raise DatabaseCorrupt(
            f"Database at {db_path} could not be read as a valid sqlite database "
            f"({error}). Relay will not operate against it, and has not modified it. "
            "Restore from backup, or run 'relay recover --from-projections' to "
            "review what a rebuild from Markdown/YAML projections would look "
            "like before applying it."
        ) from error
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
        with _refused_when_sqlite_cannot_open(conn, db_path):
            _refuse_unreadable_version(conn, db_path)
        return conn

    # Only now, having confirmed the file is healthy, open the real
    # read-write connection this process will actually use. Its version is
    # read on this connection, not the probe: immutable=1 ignores the WAL,
    # so the probe could see a stale one.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    with _refused_when_sqlite_cannot_open(conn, db_path):
        conn.execute("PRAGMA foreign_keys = ON")
        _refuse_unknown_version(conn, db_path)
        conn.execute("PRAGMA journal_mode = WAL")
    _upgrade_if_behind(conn, db_path)
    init_schema(conn)
    return conn


@contextmanager
def _refused_when_sqlite_cannot_open(conn: sqlite3.Connection, db_path: Path):
    """Turn SQLite's own error on the first reads of a new connection into
    Relay's one-line refusal, closing the connection. These reads come
    before anything is written, so nothing has changed."""
    try:
        yield
    except sqlite3.Error as error:
        conn.close()
        raise LedgerCannotBeOpened(
            f"Relay could not open the ledger at {db_path} ({error}). SQLite needs to "
            f"make {db_path.name}-wal and {db_path.name}-shm beside it when they are "
            f"missing, so {db_path.parent} must be writable by this user. Nothing was changed."
        ) from error


def _refuse_unreadable_version(conn: sqlite3.Connection, db_path: Path) -> None:
    """For a read-only connection: refuse a ledger from a newer Relay, and
    one behind this Relay that a pending step must change before it reads
    correctly. A ledger behind only by steps that leave it readable is read
    as it is."""
    _refuse_unknown_version(conn, db_path)
    found, latest = ledger_version(conn), latest_ledger_version()
    if all(upgrade.readable_before for upgrade in _pending_upgrades(found, latest)):
        return
    conn.close()
    raise LedgerNeedsUpgrade(
        f"The ledger at {db_path} is version {found} and this Relay reads version {latest}; "
        "it must be upgraded before it can be read. Run any Relay command that opens it for "
        "writing, such as 'relay list', to upgrade it (Relay backs it up first). Nothing was changed."
    )


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


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Every ledger write runs inside this. Outside any transaction it
    begins one (BEGIN IMMEDIATE, so the write lock is taken up front),
    commits when the block ends and rolls back if it raises. Inside one it
    joins it and commits nothing, so a command can make several record_*
    calls and have them land together or not at all.

    Python's own `with conn:` would commit on exit even when nested, which
    is why the ledger never uses it for a write."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------

def task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return row is not None


def _insert_task_row(conn: sqlite3.Connection, task_id: str, *, repo: str | None,
                      deploy_intent: bool, current_phase: str, created: str | None = None,
                      updated: str | None = None, worktree_path: str | None = None,
                      branch: str | None = None, base: str | None = None,
                      next_step: int = 1) -> None:
    """The actual INSERT + task_created event, with no transaction
    management of its own -- the caller owns the transaction boundary.

    This exists so create_task() and import_task() (which needs task-row
    creation to be part of ONE larger transaction alongside its
    steps/events/history writes) share the exact same insert logic. The
    optional keywords default to what a task created now has; the
    task_created event carries the task's `updated` time.
    """
    _require_configured()
    if current_phase not in _valid_phases and current_phase != "done":
        raise InvalidTransition(f"cannot create task in unknown phase '{current_phase}'")
    now = _now()
    created, updated = created or now, updated or now
    conn.execute(
        """INSERT INTO tasks
           (task_id, repo, created, updated, status, current_phase,
            next_step, deploy_intent, worktree_path, branch, base, schema_version)
           VALUES (?, ?, ?, ?, 'in_progress', ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, repo, created, updated, current_phase, next_step, int(deploy_intent),
         worktree_path, branch, base, SCHEMA_VERSION),
    )
    conn.execute(
        "INSERT INTO events (task_id, event_type, phase, timestamp) VALUES (?, ?, ?, ?)",
        (task_id, EVENT_TASK_CREATED, current_phase, updated),
    )


def create_task(conn: sqlite3.Connection, task_id: str, *, repo: str | None = None,
                deploy_intent: bool = False, current_phase: str = "architect",
                created: str | None = None, updated: str | None = None,
                worktree_path: str | None = None, branch: str | None = None,
                base: str | None = None, next_step: int = 1) -> dict:
    """Create a new task row, status in_progress, and its task_created
    event. `relay start` passes the run state it built (run_state.create_run);
    the defaults are a task created now with nothing recorded yet.

    "done" is accepted alongside the real phases -- it is the same sentinel
    Relay itself stores in run.yaml's current_phase once a workflow
    completes, not a real phase with steps/verdicts of its own.
    """
    with transaction(conn):
        _insert_task_row(conn, task_id, repo=repo, deploy_intent=deploy_intent,
                         current_phase=current_phase, created=created, updated=updated,
                         worktree_path=worktree_path, branch=branch, base=base,
                         next_step=next_step)
    return get_task(conn, task_id)


def update_task(conn: sqlite3.Connection, task_id: str, *, status: str | None,
                current_phase: str | None, next_step: int | None, updated: str | None,
                deploy_intent: bool, repo: str | None, worktree_path: str | None,
                branch: str | None, base: str | None) -> None:
    """Write the task row's own fields: what save_run_state() saves. A None
    status, phase or next step keeps the stored one; `updated` defaults to
    now. A state_saved event is written only when the status or the current
    phase actually changes, with both as [before, after].

    Steps, verdicts and retries are not task fields and are never written
    here: each has its own call (record_dispatch, record_verdict,
    record_retry)."""
    _require_configured()
    if current_phase and current_phase != "done" and current_phase not in _valid_phases:
        raise InvalidTransition(f"cannot save task in unknown phase '{current_phase}'")
    if status and status not in _valid_statuses:
        raise InvalidTransition(f"cannot save task with unknown status '{status}'")
    when = updated or _now()
    with transaction(conn):
        before = conn.execute("SELECT status, current_phase, next_step FROM tasks WHERE task_id = ?",
                              (task_id,)).fetchone()
        if before is None:
            raise ValueError(f"no such task: {task_id}")
        after_status = status or before["status"]
        after_phase = current_phase or before["current_phase"]
        conn.execute(
            """UPDATE tasks SET repo=?, updated=?, status=?, current_phase=?,
                   next_step=?, deploy_intent=?, worktree_path=?, branch=?, base=?
               WHERE task_id=?""",
            (repo, when, after_status, after_phase,
             before["next_step"] if next_step is None else next_step, int(bool(deploy_intent)),
             worktree_path, branch, base, task_id),
        )
        if (after_status, after_phase) == (before["status"], before["current_phase"]):
            return
        payload = json.dumps({"status": [before["status"], after_status],
                              "current_phase": [before["current_phase"], after_phase]})
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, payload, timestamp) VALUES (?,?,?,?,?)",
            (task_id, EVENT_STATE_SAVED, after_phase, payload, when),
        )


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


def get_retry_counts(conn: sqlite3.Connection, task_id: str) -> dict[str, int]:
    """Automatic rounds into each phase since the owner last decided, read
    from the task's events in the order they were written.

    A retry event (Relay routed the task back on its own) adds one to its
    phase. A send-back or an override into a phase is the owner's decision
    and starts that phase's count again; so does an override out of a stop
    at the limit, for the phase the limit stopped. Nothing is stored, so an
    older ledger is read by the same rule; there a send-back's own retry
    event sits just before its sent_back event, which clears it.

    builder and reviewer are always present, at 0 when nothing counted, so
    run.yaml keeps the same shape."""
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
    text = report_text(path)
    if text is None:
        return None
    try:
        fm = _parse_frontmatter_text(text)
    except ValueError:  # a frontmatter that cannot be read: no verdict, as with none
        return None
    verdict = fm.get("verdict")
    if not verdict or verdict not in _valid_verdicts.get(phase, []):
        return None
    content_hash = _hash_file(path)
    with transaction(conn):
        conn.execute(
            "UPDATE steps SET verdict=?, content_hash=? WHERE task_id=? AND step=?",
            (verdict, content_hash, task_id, step),
        )
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, verdict, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_VERDICT_RECORDED, phase, step, verdict, _now()),
        )
    return verdict


def report_text(path: Path) -> str | None:
    """A report file's text, or None when there is no regular file at
    `path` (none, a folder, a named pipe) or its bytes are not UTF-8. A
    report Relay cannot read has no verdict, as one with no frontmatter."""
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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


def _require_phase(phase: str) -> None:
    _require_configured()
    if phase not in _valid_phases:
        raise InvalidTransition(f"unknown phase '{phase}'")


def record_dispatch(conn: sqlite3.Connection, task_id: str, *, phase: str, step: int,
                    artifact_filename: str, created_at: str, task_dir: Path | None = None) -> None:
    """Record that a step was started: its phase, its number, and the report
    file its agent was told to write, exactly as the prompt named it. The
    verdict is not known yet (NULL). With task_dir, the report's hash is
    stored if the file is already there. A step number used twice raises."""
    _require_phase(phase)
    content_hash = _hash_file(task_dir / artifact_filename) if task_dir is not None else None
    with transaction(conn):
        conn.execute(
            """INSERT INTO steps (task_id, step, phase, verdict, artifact_filename,
                   content_hash, created_at) VALUES (?,?,?,NULL,?,?,?)""",
            (task_id, step, phase, artifact_filename, content_hash, created_at),
        )


def record_verdict(conn: sqlite3.Connection, task_id: str, *, step: int, phase: str,
                   verdict: str, task_dir: Path | None = None) -> None:
    """Record a step's verdict. Writes only when it differs from the stored
    one; with task_dir, the report's hash is refreshed from the file the
    step row names. The step must exist and be of `phase`."""
    _require_phase(phase)
    if verdict not in _valid_verdicts.get(phase, []):
        raise InvalidTransition(f"cannot record verdict '{verdict}' for phase '{phase}'")
    with transaction(conn):
        row = conn.execute("SELECT phase, verdict, artifact_filename FROM steps "
                           "WHERE task_id = ? AND step = ?", (task_id, step)).fetchone()
        if row is None or row["phase"] != phase:
            raise ValueError(f"no {phase} step {step} for task {task_id}")
        if row["verdict"] == verdict:
            return
        content_hash = _hash_file(task_dir / row["artifact_filename"]) if task_dir is not None else None
        conn.execute(
            "UPDATE steps SET verdict=?, content_hash=COALESCE(?, content_hash) "
            "WHERE task_id=? AND step=?",
            (verdict, content_hash, task_id, step),
        )


def record_retry(conn: sqlite3.Connection, task_id: str, *, phase: str) -> None:
    """Record one automatic round into `phase`: Relay routed the task back
    on its own. get_retry_counts() counts these."""
    _require_phase(phase)
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, timestamp) VALUES (?,?,?,?)",
            (task_id, EVENT_RETRY, phase, _now()),
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
    with transaction(conn):
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
    with transaction(conn):
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
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_TEST_RUN, phase, step, json.dumps(run), _now()),
        )


def record_test_command_accepted(conn: sqlite3.Connection, task_id: str, *, phase: str,
                                 step: int | None, command: str, test_report: str | None,
                                 commit: str | None, harness_digest: str | None = None,
                                 tests_run: int | None = None,
                                 harness_unchecked_at: str | None = None) -> None:
    """Record that the owner accepted what a passing test run flagged: a
    test command the task's base branch does not have, and, when the run
    flagged them, its changed harness files (by digest), its number of
    tests run, and the commit whose harness Relay could not check. Its own event, tied to exactly what was shown, so no other
    approval can stand in for it. The later keys are written only when set,
    so a command-only acceptance holds just its three keys."""
    accepted = {"command": command, "test_report": test_report, "commit": commit}
    if harness_digest is not None:
        accepted["harness_digest"] = harness_digest
    if tests_run is not None:
        accepted["tests_run"] = tests_run
    if harness_unchecked_at is not None:
        accepted["harness_unchecked_at"] = harness_unchecked_at
    payload = json.dumps(accepted)
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_TEST_COMMAND_ACCEPTED, phase, step, payload, _now()),
        )


def record_owner_note(conn: sqlite3.Connection, task_id: str, *, phase: str | None,
                      step: int | None, note: str) -> None:
    """Record something the owner said about the task without moving it:
    a decision, a correction, a fact. Later agents are given it."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_OWNER_NOTE, phase, step, json.dumps({"note": note}), _now()),
        )


def record_slept_during_step(conn: sqlite3.Connection, task_id: str, *, phase: str,
                             step: int | None, sleep: dict) -> None:
    """Record that the machine went to sleep while a step was open, so the
    time the step took can be read for what it was. `sleep` holds how many
    times it slept, for how long in all, and when the first and last began."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_SLEPT_DURING_STEP, phase, step, json.dumps(sleep), _now()),
        )


def record_time_limit_stop(conn: sqlite3.Connection, task_id: str, *, phase: str,
                           step: int | None, limit_seconds: int, ran_seconds: int) -> None:
    """Record that Relay stopped a role started under --auto because it ran
    past its time limit: the limit it had and how long it ran, in seconds."""
    payload = json.dumps({"limit_seconds": limit_seconds, "ran_seconds": ran_seconds})
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_TIME_LIMIT_STOP, phase, step, payload, _now()),
        )


def record_arbiter_started(conn: sqlite3.Connection, task_id: str, *, step: int, report: str,
                           situation: str, trigger: dict) -> None:
    """Record that the Arbiter was dispatched: its step, the report it
    writes, the situation it is called in and the report it rules on
    (`trigger`: {step, phase, verdict})."""
    payload = json.dumps({"report": report, "situation": situation, "trigger": trigger})
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_ARBITER_STARTED, "arbiter", step, payload, _now()),
        )


def record_arbiter_ruling(conn: sqlite3.Connection, task_id: str, *, step: int, report: str,
                          situation: str, ruling: dict, routed: bool, outcome: str) -> None:
    """Record the Arbiter's ruling as Relay acted on it. The verdict column
    holds the decision; `ruling` holds the checked fields only, never the
    report's prose. `routed` is True when Relay moved the task on it, and
    `outcome` is Relay's one line on what it did."""
    payload = json.dumps({"report": report, "situation": situation, "ruling": ruling,
                          "routed": routed, "outcome": outcome})
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, verdict, payload, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, EVENT_ARBITER_RULED, "arbiter", step, ruling.get("decision"), payload, _now()),
        )


def record_arbiter_unreadable(conn: sqlite3.Connection, task_id: str, *, step: int, report: str,
                              problem: str) -> None:
    """Record that Relay could not act on an Arbiter's report, and why."""
    payload = json.dumps({"report": report, "problem": problem})
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_ARBITER_UNREADABLE, "arbiter", step, payload, _now()),
        )


def record_follow_ups(conn: sqlite3.Connection, task_id: str, *, step: int, phase: str,
                      report: str, items: list[dict]) -> None:
    """Record the findings the owner accepted and logged as follow-ups at a
    gate. Each item is {kind, text, severity, from}."""
    payload = json.dumps({"report": report, "items": items})
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_FOLLOW_UPS_LOGGED, phase, step, payload, _now()),
        )


def record_deploy_decision(conn: sqlite3.Connection, task_id: str, *, step: int | None,
                           previous: bool, decided: bool) -> None:
    """Record the Architect's deploy decision when it changes the task's
    deploy intent, so the switch is part of the task's history."""
    payload = json.dumps({"previous": previous, "decided": decided, "source": "architect"})
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_DEPLOY_DECIDED, "architect", step, payload, _now()),
        )


def record_sizing_change(conn: sqlite3.Connection, task_id: str, *, before: dict, after: dict,
                         reason: str, by: str, phase: str | None, step: int | None) -> None:
    """Record a change to the task's sizing as a further `sized` event.
    `before` and `after` are {size, risky, deploy}. The top-level keys are
    the start event's, so any reader of `sized` reads the new sizing;
    `from`, `reason` and `trigger` ({by, step}, set by Relay, never read
    from a report) say what changed it."""
    payload = json.dumps({"size": after["size"], "risky": after["risky"], "deploy": after.get("deploy"),
                          "note": None, "from": {"size": before["size"], "risky": before["risky"]},
                          "reason": reason, "trigger": {"by": by, "step": step}})
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_SIZED, phase, step, payload, _now()),
        )


def record_risk_hint(conn: sqlite3.Connection, task_id: str, *, step: int | None, hint: dict) -> None:
    """Record the sizing hint worked out after the Builder step `step`."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_RISK_HINT, "builder", step, json.dumps(hint), _now()),
        )


def record_sizing_judgment(conn: sqlite3.Connection, task_id: str, *, phase: str, step: int | None,
                           judgment: dict) -> None:
    """Record what a judging role's report said of the sizing: {by, answer,
    reason, refused, unanswered, hint: {category: count}}."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, phase, step, payload, timestamp) VALUES (?,?,?,?,?,?)",
            (task_id, EVENT_SIZING_JUDGED, phase, step, json.dumps(judgment), _now()),
        )


def record_base_confirmed(conn: sqlite3.Connection, task_id: str, *, base: str, ref: str | None,
                          commit: str, previous: str | None, how: str) -> None:
    """Record the commit Relay confirmed for the task's base: `base` is the
    name the owner gave, `ref` the full ref it was confirmed through (None
    for a commit id the owner typed), `previous` the commit it moved from,
    and `how` one of "start", "first" or "moved"."""
    payload = {"base": base, "ref": ref, "commit": commit, "from": previous, "how": how}
    with transaction(conn):
        conn.execute(
            "INSERT INTO events (task_id, event_type, payload, timestamp) VALUES (?,?,?,?)",
            (task_id, EVENT_BASE_CONFIRMED, json.dumps(payload), _now()),
        )


def latest_base_confirmed(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """The payload of the newest base_confirmed event for the task, or None
    when it has none or the payload is not a JSON object."""
    row = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND event_type = ? ORDER BY id DESC LIMIT 1",
        (task_id, EVENT_BASE_CONFIRMED),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def record_event(conn: sqlite3.Connection, task_id: str, event_type: str, payload: dict) -> None:
    """Record one event with a JSON payload and no phase or step."""
    with transaction(conn):
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
    with transaction(conn):
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
            d["payload"] = _event_payload(task_id, r["event_type"], r["payload"])
        out.append(d)
    return out


def _event_payload(task_id: str, event_type: str, payload: str):
    """An event's payload, read back from its JSON. Relay writes only JSON,
    so one it cannot read (not JSON, or nested too deep) is a hand-edited
    row, raised as UnreadableEvent rather than guessed at."""
    try:
        return json.loads(payload)
    except (ValueError, RecursionError):
        raise UnreadableEvent(
            f"an event of task {task_id} ({event_type}) has a payload Relay cannot read as JSON, "
            f"so it cannot read the task's record. Relay never writes one; a hand edit of the "
            f"ledger does. Correct or remove that row.") from None


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
# Report frontmatter, read by the verdict sync above (and by migrate_legacy)
# ---------------------------------------------------------------------------

def _parse_frontmatter_text(text: str) -> dict:
    """Frontmatter as a dict; {} when there is none. Raises ValueError when
    a frontmatter block exists but isn't a readable YAML mapping, or holds
    what bounded_yaml does not read."""
    try:
        return bounded_yaml.frontmatter(text)
    except bounded_yaml.NotRead as problem:
        raise ValueError(f"frontmatter {problem}") from problem
