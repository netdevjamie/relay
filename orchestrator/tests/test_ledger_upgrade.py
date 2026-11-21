"""The ledger knows its version and upgrades itself, after a backup.

A ledger records its version in SQLite's user_version. The first open for
writing by a Relay that knows a later version backs the file up beside
itself and applies the pending steps in order, in one transaction. A
ledger from a newer Relay is refused untouched, and a read-only open never
writes or backs up.

Every ledger here is built in a temp folder; the old-schema one from
fixtures/ledger_v0_schema.sql, the schema exactly as it was before ledger
versions, so it does not depend on today's code.
"""

import json
import os
import signal
import stat
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import db
import relay
import settings
from settings import DEFAULT_CONFIG, VALID_PHASES, VALID_STATUSES, VALID_VERDICTS

TESTS = Path(__file__).resolve().parent
V0_SCHEMA = (TESTS / "fixtures" / "ledger_v0_schema.sql").read_text()


@pytest.fixture(autouse=True)
def enums():
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)


@pytest.fixture
def ledger(tmp_path) -> Path:
    return tmp_path / "relay.db"


def _event(connection, event_type: str, payload: dict | None = None, *, phase=None, step=None,
           verdict=None, at="2026-10-01T00:00:00Z") -> None:
    connection.execute(
        "INSERT INTO events (task_id, event_type, phase, step, verdict, payload, timestamp) "
        "VALUES ('old', ?, ?, ?, ?, ?, ?)",
        (event_type, phase, step, verdict, json.dumps(payload) if payload else None, at))


def build_v0_ledger(path: Path) -> None:
    """A ledger as Relay wrote it before ledger versions: user_version 0,
    WAL mode, one task with steps, retries, an override, a send-back, a
    save, and one of each legacy and migration-only event type."""
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(V0_SCHEMA)
    with connection:
        connection.execute(
            "INSERT INTO tasks VALUES ('old', 'relay', '2026-10-01T00:00:00Z', "
            "'2026-10-02T00:00:00Z', 'in_progress', 'builder', 5, 1, '/w', 'b', 'origin/main', 1)")
        for step, phase, verdict, name in [(1, "architect", "ready-for-builder", "01-architect.md"),
                                           (2, "builder", "ready-for-review", "02-builder.md"),
                                           (3, "reviewer", "changes_requested", "03-reviewer.md"),
                                           (4, "builder", None, "04-builder-fix-1.md")]:
            connection.execute("INSERT INTO steps VALUES ('old', ?, ?, ?, ?, NULL, ?)",
                               (step, phase, verdict, name, "2026-10-01T00:00:00Z"))
        _event(connection, db.EVENT_TASK_CREATED, phase="architect")
        _event(connection, db.EVENT_STATE_SAVED, {"status": ["in_progress", "in_progress"],
                                                  "current_phase": ["architect", "architect"]},
               phase="architect")
        _event(connection, db.EVENT_RETRY, phase="builder")
        _event(connection, db.EVENT_RETRY, phase="reviewer")
        _event(connection, db.EVENT_APPROVAL, {"next_phase": "builder", "method": "override",
                                               "note": None, "stopped_phase": "reviewer"},
               phase="reviewer")
        _event(connection, db.EVENT_SENT_BACK, {"to_phase": "builder", "note": "Fix it."},
               phase="reviewer", verdict="changes_requested")
        _event(connection, db.EVENT_RETRY, phase="builder")
        for legacy in sorted(db.LEGACY_EVENT_TYPES | db.MIGRATION_EVENT_TYPES):
            _event(connection, legacy, {"from": "an older Relay"}, step=2)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    connection.close()


def build_current_ledger(path: Path) -> None:
    db.init_database(path).close()


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.backup-*"))


def version_of(path: Path) -> int:
    probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return probe.execute("PRAGMA user_version").fetchone()[0]
    finally:
        probe.close()


def read_everything(connection) -> list:
    """The task as every reader sees it: get_task, get_history,
    get_retry_counts and get_events."""
    return [db.get_task(connection, "old"), db.get_history(connection, "old"),
            db.get_retry_counts(connection, "old"), db.get_events(connection, "old")]


def read_only_view(path: Path) -> list:
    connection = db.connect_existing(path, read_only=True)
    try:
        return read_everything(connection)
    finally:
        connection.close()


def add_upgrade(monkeypatch, to_version: int, apply, readable_before: bool = True) -> None:
    monkeypatch.setattr(db, "_UPGRADES", db._UPGRADES + [db.Upgrade(to_version, apply,
                                                                    readable_before)])


# ---------------------------------------------------------------------------
# 12. Stamped, backed up, upgraded
# ---------------------------------------------------------------------------

def test_a_fresh_ledger_is_stamped_latest_with_no_backup(ledger):
    build_current_ledger(ledger)
    assert version_of(ledger) == db.latest_ledger_version() == db.LEDGER_VERSION == 1
    assert backups(ledger) == []


def test_an_old_ledger_is_backed_up_then_upgraded(ledger):
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()
    before = read_only_view(ledger)
    assert not Path(f"{ledger}-wal").exists() or Path(f"{ledger}-wal").stat().st_size == 0

    db.connect_existing(ledger).close()

    assert version_of(ledger) == db.latest_ledger_version()
    (backup,) = backups(ledger)
    assert backup.name.startswith("relay.db.backup-v0-")
    assert backup.read_bytes() == bytes_before
    assert read_only_view(ledger) == before


def test_a_ledger_with_wal_frames_is_backed_up_through_the_backup_api(ledger):
    """Another process has committed writes that are still only in the WAL:
    the main file alone is not the ledger, so it is copied through SQLite."""
    build_v0_ledger(ledger)
    other = sqlite3.connect(str(ledger))
    other.execute("PRAGMA wal_autocheckpoint = 0")
    with other:
        other.execute("UPDATE tasks SET current_phase = 'reviewer' WHERE task_id = 'old'")
    assert Path(f"{ledger}-wal").stat().st_size > 0
    before = read_only_view(ledger)
    try:
        db.connect_existing(ledger).close()
    finally:
        other.close()

    (backup,) = backups(ledger)
    probe = sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
    probe.row_factory = sqlite3.Row
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 0
        assert read_everything(probe) == before
        assert db.get_task(probe, "old")["current_phase"] == "reviewer"
    finally:
        probe.close()
    assert version_of(ledger) == db.latest_ledger_version()


def test_a_current_ledger_opens_with_no_backup_and_no_write(ledger):
    build_current_ledger(ledger)
    bytes_before, mtime_before = ledger.read_bytes(), ledger.stat().st_mtime_ns
    db.connect_existing(ledger).close()
    db.connect(ledger).close()
    assert (ledger.read_bytes(), ledger.stat().st_mtime_ns) == (bytes_before, mtime_before)
    assert backups(ledger) == []


def test_upgrade_steps_run_in_order_and_once(ledger, monkeypatch):
    build_current_ledger(ledger)
    applied = []

    def step(number):
        def apply(connection):
            applied.append(number)
            connection.execute(f"CREATE TABLE probe_{number} (x INTEGER)")
        return apply
    add_upgrade(monkeypatch, 2, step(2))
    add_upgrade(monkeypatch, 3, step(3))

    db.connect_existing(ledger).close()
    assert applied == [2, 3]
    assert version_of(ledger) == 3
    (backup,) = backups(ledger)
    assert backup.name.startswith("relay.db.backup-v1-")

    db.connect_existing(ledger).close()
    db.connect(ledger).close()
    assert applied == [2, 3]
    assert len(backups(ledger)) == 1


# ---------------------------------------------------------------------------
# 13. A failed upgrade changes nothing
# ---------------------------------------------------------------------------

def test_a_failing_step_leaves_the_ledger_as_it_was(ledger, monkeypatch):
    build_current_ledger(ledger)

    def half_done(connection):
        connection.execute("CREATE TABLE half_done (x INTEGER)")
        connection.execute("INSERT INTO half_done VALUES (1)")
        raise RuntimeError("the step broke")
    add_upgrade(monkeypatch, 2, half_done)

    with pytest.raises(db.LedgerUpgradeFailed) as refused:
        db.connect_existing(ledger)
    (backup,) = backups(ledger)
    assert str(backup) in str(refused.value)
    assert "Nothing was changed" in str(refused.value)
    assert version_of(ledger) == 1
    probe = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master")}
    finally:
        probe.close()
    assert "half_done" not in tables


def test_a_backup_that_cannot_be_made_stops_the_upgrade(ledger, monkeypatch):
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()

    def no_copy(source, copy, *args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(db.shutil, "copyfileobj", no_copy)

    with pytest.raises(db.LedgerUpgradeFailed, match="could not back up.*Nothing was changed"):
        db.connect_existing(ledger)
    assert backups(ledger) == []
    assert version_of(ledger) == 0
    assert ledger.read_bytes() == bytes_before


def _cut_short(data: bytes) -> bytes:
    return data[:len(data) // 2]


def _stamped_version_5(data: bytes) -> bytes:
    """The same file with another user_version: SQLite keeps it as four
    big-endian bytes at offset 60 of the header."""
    return data[:60] + (5).to_bytes(4, "big") + data[64:]


@pytest.mark.parametrize("spoil", [_cut_short, _stamped_version_5],
                         ids=["cut short", "another version"])
def test_a_backup_that_comes_out_wrong_stops_the_upgrade(ledger, monkeypatch, spoil):
    """The copy is made but is not the ledger: Relay's check of the copy
    catches it before any step or the version stamp, and removes it."""
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()

    def spoiled_copy(source, copy, *args, **kwargs):
        copy.write(spoil(source.read()))
    monkeypatch.setattr(db.shutil, "copyfileobj", spoiled_copy)

    with pytest.raises(db.LedgerUpgradeFailed, match="could not back up.*Nothing was changed"):
        db.connect_existing(ledger)
    assert backups(ledger) == []
    assert version_of(ledger) == 0
    assert ledger.read_bytes() == bytes_before


# ---------------------------------------------------------------------------
# 14. Newer refused; read-only never writes
# ---------------------------------------------------------------------------

def _stamp(path: Path, version: int) -> None:
    connection = sqlite3.connect(str(path))
    connection.execute(f"PRAGMA user_version = {version}")
    connection.close()


@pytest.mark.parametrize("version, refusal", [(2, db.LedgerTooNew), (2**31 - 1, db.LedgerTooNew),
                                              (-1, db.DatabaseCorrupt)])
def test_a_ledger_of_a_version_relay_does_not_know_is_refused_unchanged(
        ledger, tmp_path, monkeypatch, capsys, version, refusal):
    build_current_ledger(ledger)
    _stamp(ledger, version)
    bytes_before = ledger.read_bytes()

    for open_it in (lambda: db.connect_existing(ledger), lambda: db.connect(ledger),
                    lambda: db.connect_existing(ledger, read_only=True)):
        with pytest.raises(refusal):
            open_it()
    assert ledger.read_bytes() == bytes_before
    assert backups(ledger) == []

    monkeypatch.setattr(settings, "_db_connection", None)
    monkeypatch.setenv("RELAY_DB_PATH", str(ledger))
    config = dict(DEFAULT_CONFIG, persistence_backend="sqlite")
    capsys.readouterr()
    assert commands.run_json_command("list", None, config) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["error"]["code"] == "ledger_unavailable"
    assert ledger.read_bytes() == bytes_before


def test_a_newer_ledger_says_to_upgrade_relay(ledger):
    build_current_ledger(ledger)
    _stamp(ledger, 5)
    with pytest.raises(db.LedgerTooNew, match="version 5.*up to 1.*Upgrade Relay.*not changed"):
        db.connect_existing(ledger)


def test_read_only_never_backs_up(ledger, tmp_path, monkeypatch):
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()
    view = read_only_view(ledger)
    assert view[0]["current_phase"] == "builder"
    assert view[2] == {"builder": 1, "reviewer": 0}
    assert version_of(ledger) == 0
    assert backups(ledger) == []
    assert ledger.read_bytes() == bytes_before

    current = tmp_path / "current.db"
    build_current_ledger(current)
    add_upgrade(monkeypatch, 2, lambda connection: None, readable_before=False)
    with pytest.raises(db.LedgerNeedsUpgrade, match="relay list"):
        db.connect_existing(current, read_only=True)
    assert version_of(current) == 1
    assert backups(current) == []


# ---------------------------------------------------------------------------
# 15. Two processes, one upgrade
# ---------------------------------------------------------------------------

def test_two_processes_upgrade_once(ledger, tmp_path):
    build_current_ledger(ledger)
    barrier = tmp_path / "go"
    environment = dict(os.environ, RELAY_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    workers = [subprocess.Popen([sys.executable, str(TESTS / "ledger_upgrade_worker.py"),
                                 str(ledger), str(barrier)],
                                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
               for _ in range(2)]
    barrier.touch()
    results = [worker.communicate(timeout=60) for worker in workers]
    assert [worker.returncode for worker in workers] == [0, 0], results

    assert len(backups(ledger)) == 1
    assert version_of(ledger) == 2
    probe = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        assert probe.execute("SELECT COUNT(*) FROM upgrade_probe").fetchone()[0] == 1
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# 16. Backups are never overwritten
# ---------------------------------------------------------------------------

class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)


def test_backups_never_overwrite(ledger, tmp_path, monkeypatch):
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()
    monkeypatch.setattr(db, "datetime", _FrozenClock)
    taken = ledger.with_name("relay.db.backup-v0-20261121T120000Z")
    taken.write_bytes(b"an earlier backup")
    elsewhere = tmp_path / "not-a-backup"
    elsewhere.write_bytes(b"leave me alone")
    planted = ledger.with_name(f"{taken.name}-2")
    planted.symlink_to(elsewhere)

    db.connect_existing(ledger).close()

    assert taken.read_bytes() == b"an earlier backup"
    assert elsewhere.read_bytes() == b"leave me alone" and planted.is_symlink()
    written = ledger.with_name(f"{taken.name}-3")
    assert written.read_bytes() == bytes_before
    assert version_of(ledger) == db.latest_ledger_version()


# ---------------------------------------------------------------------------
# A backup name only ever holds a whole, checked backup
# ---------------------------------------------------------------------------

def test_a_kill_mid_copy_leaves_no_file_under_a_backup_name(ledger, tmp_path):
    """Relay is killed with half the copy written: nothing may be left under
    a backup name, since a later restore would pick up a broken file."""
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()
    environment = dict(os.environ, RELAY_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    worker = subprocess.run([sys.executable, str(TESTS / "ledger_backup_crash_worker.py"),
                             str(ledger)], env=environment, capture_output=True, timeout=60)
    assert worker.returncode == -signal.SIGKILL, worker

    assert backups(ledger) == []
    assert version_of(ledger) == 0
    assert ledger.read_bytes() == bytes_before
    (unfinished,) = ledger.parent.glob(".relay.db.unfinished-backup-*")

    db.connect_existing(ledger).close()
    (backup,) = backups(ledger)
    assert backup.read_bytes() == bytes_before
    assert version_of(ledger) == db.latest_ledger_version()
    assert list(ledger.parent.glob(".relay.db.unfinished-backup-*")) == [unfinished]


@pytest.fixture
def locked_folder_ledger(tmp_path):
    """A behind ledger in a folder nobody may create files in. A reader stays
    open so the ledger's -wal and -shm files exist (Linux's SQLite removes
    them when the last connection closes, and they cannot be made in this
    folder), letting Relay open the ledger and reach the backup. The folder
    is made writable again afterwards so pytest can clean it up."""
    if os.geteuid() == 0:
        pytest.skip("root can create files in any folder")
    folder = tmp_path / "locked"
    folder.mkdir()
    ledger = folder / "relay.db"
    build_v0_ledger(ledger)
    reader = sqlite3.connect(str(ledger))
    reader.execute("SELECT COUNT(*) FROM tasks").fetchone()
    folder.chmod(0o555)
    try:
        yield ledger
    finally:
        folder.chmod(0o755)
        reader.close()


def test_a_backup_folder_that_cannot_be_written_refuses_with_relays_message(
        locked_folder_ledger):
    ledger = locked_folder_ledger
    bytes_before = ledger.read_bytes()

    with pytest.raises(db.LedgerUpgradeFailed, match="could not back up.*Nothing was changed"):
        db.connect_existing(ledger)
    assert {path.name for path in ledger.parent.iterdir()} == {
        "relay.db", "relay.db-shm", "relay.db-wal"}
    assert version_of(ledger) == 0
    assert ledger.read_bytes() == bytes_before


def test_relay_list_in_a_folder_that_cannot_be_written_prints_one_line_and_no_traceback(
        locked_folder_ledger, tmp_path, monkeypatch, capsys):
    ledger = locked_folder_ledger
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(settings, "_db_connection", None)
    monkeypatch.setenv("RELAY_DB_PATH", str(ledger))
    (tmp_path / "tasks").mkdir()
    config = dict(DEFAULT_CONFIG, persistence_backend="sqlite")
    monkeypatch.setattr(settings, "load_config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["relay", "list"])

    with pytest.raises(SystemExit) as stopped:
        relay.main()
    assert stopped.value.code == 1
    printed = capsys.readouterr()
    assert "Traceback" not in printed.out + printed.err
    (line,) = printed.out.splitlines()
    assert line.startswith("Error: Relay could not back up the ledger")
    assert "Nothing was changed" in line
    assert version_of(ledger) == 0


def _swap_new_empty_files_for_symlinks(folder: Path, ledger: Path, victim: Path) -> None:
    """What something racing Relay in the ledger's folder would do: every
    empty file Relay has just created there becomes a symlink to the victim."""
    own = {ledger.name, f"{ledger.name}-wal", f"{ledger.name}-shm"}
    for path in folder.iterdir():
        if path.name in own or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size == 0:
            path.unlink()
            path.symlink_to(victim)


def test_a_symlink_swapped_in_during_the_backup_is_never_followed(ledger, tmp_path, monkeypatch):
    """With unmerged WAL frames the copy goes through SQLite. Whatever Relay
    has created in the folder is swapped for a symlink to an empty file
    elsewhere each time Relay opens a connection: the copy must never land
    in that file, and no backup name may be left as a symlink."""
    build_v0_ledger(ledger)
    other = sqlite3.connect(str(ledger))
    other.execute("PRAGMA wal_autocheckpoint = 0")
    with other:
        other.execute("UPDATE tasks SET current_phase = 'reviewer' WHERE task_id = 'old'")
    victim = tmp_path / "elsewhere" / "victim"
    victim.parent.mkdir()
    victim.write_bytes(b"")
    real_connect = sqlite3.connect

    def racing_connect(*args, **kwargs):
        _swap_new_empty_files_for_symlinks(ledger.parent, ledger, victim)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", racing_connect)
    try:
        try:
            db.connect_existing(ledger).close()
        except db.LedgerUpgradeFailed:
            pass
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)
        other.close()

    assert victim.read_bytes() == b""
    assert [path for path in backups(ledger) if path.is_symlink()] == []
    for backup in backups(ledger):
        probe = sqlite3.connect(f"file:{backup}?mode=ro&immutable=1", uri=True)
        try:
            assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert probe.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            probe.close()


def test_a_symlink_planted_at_the_unfinished_name_is_never_followed(ledger, tmp_path, monkeypatch):
    """Something has guessed the next unfinished-backup name and planted a
    symlink there: Relay must not write through it, and picks another name."""
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()
    victim = tmp_path / "elsewhere"
    victim.write_bytes(b"leave me alone")
    planted = ledger.with_name(".relay.db.unfinished-backup-guessed")
    planted.symlink_to(victim)
    names = iter(["guessed", "fresh"])
    monkeypatch.setattr(db.secrets, "token_hex", lambda size: next(names))

    try:
        db.connect_existing(ledger).close()
    finally:  # the victim is checked first, even when the open refuses
        assert victim.read_bytes() == b"leave me alone" and planted.is_symlink()
    (backup,) = backups(ledger)
    assert backup.read_bytes() == bytes_before
    assert not ledger.with_name(".relay.db.unfinished-backup-fresh").exists()


@pytest.mark.parametrize("ledger_mode, umask, expected", [(0o600, 0o022, 0o600),
                                                          (0o640, 0o077, 0o600),
                                                          (0o644, 0o022, 0o644)])
def test_a_backup_is_never_more_readable_than_the_ledger(ledger, ledger_mode, umask, expected):
    build_v0_ledger(ledger)
    ledger.chmod(ledger_mode)
    previous = os.umask(umask)
    try:
        db.connect_existing(ledger).close()
    finally:
        os.umask(previous)
    (backup,) = backups(ledger)
    assert stat.S_IMODE(backup.stat().st_mode) == expected


def test_when_every_backup_name_is_taken_nothing_is_left_behind(ledger, monkeypatch):
    """The copy is made and checked, but has no free name to go to: the
    upgrade is refused and the unfinished copy is removed."""
    build_v0_ledger(ledger)
    bytes_before = ledger.read_bytes()
    monkeypatch.setattr(db, "datetime", _FrozenClock)
    first = "relay.db.backup-v0-20261121T120000Z"
    for attempt in range(1, 1000):
        ledger.with_name(first if attempt == 1 else f"{first}-{attempt}").write_bytes(b"taken")

    with pytest.raises(db.LedgerUpgradeFailed, match="no free name.*Nothing was changed"):
        db.connect_existing(ledger)
    assert list(ledger.parent.glob(".relay.db.unfinished-backup-*")) == []
    assert all(path.read_bytes() == b"taken" for path in backups(ledger))
    assert version_of(ledger) == 0
    assert ledger.read_bytes() == bytes_before
