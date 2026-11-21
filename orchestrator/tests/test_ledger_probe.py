"""The corruption check in db.connect_existing never calls a healthy ledger
corrupt because another process is writing it, and never changes a ledger
it does call corrupt.

The check reads the main file alone, through an immutable connection that
takes no lock and ignores the WAL. While another Relay process checkpoints
its writes into the main file, that read can meet a page mid-copy. Every
ledger here is built in a temp folder.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from settings import VALID_PHASES, VALID_STATUSES, VALID_VERDICTS

TESTS = Path(__file__).resolve().parent
CHURN_SECONDS = 2.0
# Byte offset of the freelist page count in an SQLite file's header.
FREELIST_COUNT_OFFSET = 36


@pytest.fixture(autouse=True)
def enums():
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)


@pytest.fixture
def ledger(tmp_path) -> Path:
    path = tmp_path / "relay.db"
    db.init_database(path).close()
    return path


class Sqlite3Watch:
    """Stands in for db's sqlite3 module. Records every database string
    connect is given, and calls before_probe(n) just before the n-th
    immutable connection opens."""

    def __init__(self, before_probe=None):
        self.opened: list[str] = []
        self._before_probe = before_probe

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, database, *args, **kwargs):
        self.opened.append(str(database))
        if self._before_probe is not None and "immutable=1" in str(database):
            self._before_probe(sum("immutable=1" in opened for opened in self.opened))
        return sqlite3.connect(database, *args, **kwargs)


def mid_write_copy_of(healthy: bytes) -> bytes:
    """The ledger's bytes as a reader can see them part way through a
    checkpoint: the header's freelist count no longer matches the pages."""
    torn = bytearray(healthy)
    torn[FREELIST_COUNT_OFFSET:FREELIST_COUNT_OFFSET + 4] = (999).to_bytes(4, "big")
    return bytes(torn)


def test_a_healthy_ledger_read_mid_write_is_not_called_corrupt(ledger, monkeypatch):
    healthy = ledger.read_bytes()
    ledger.write_bytes(mid_write_copy_of(healthy))

    def writer_finishes_before_the_next_probe(probe_number: int) -> None:
        if probe_number == 2:
            ledger.write_bytes(healthy)

    watch = Sqlite3Watch(before_probe=writer_finishes_before_the_next_probe)
    monkeypatch.setattr(db, "sqlite3", watch)

    db.connect_existing(ledger, read_only=True).close()

    assert sum("immutable=1" in opened for opened in watch.opened) == 2


def test_a_concurrent_writer_never_makes_a_healthy_ledger_look_corrupt(ledger, tmp_path):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    writer = subprocess.Popen([sys.executable, str(TESTS / "ledger_churn_worker.py"),
                               str(ledger), str(CHURN_SECONDS)],
                              env=environment, stderr=subprocess.PIPE)
    opens, false_alarms = 0, []
    try:
        while writer.poll() is None:
            opens += 1
            try:
                db.connect_existing(ledger, read_only=True).close()
            except db.DatabaseCorrupt as error:
                false_alarms.append(str(error))
    finally:
        _, writer_errors = writer.communicate(timeout=60)

    assert writer.returncode == 0, writer_errors
    assert opens > 50
    assert false_alarms == [], f"{len(false_alarms)} of {opens} opens: {false_alarms[0]}"


def test_a_corrupt_ledger_and_its_stale_side_files_are_left_untouched(tmp_path, monkeypatch):
    ledger = tmp_path / "relay.db"
    files = {ledger: b"garbage, not a real sqlite file" * 100,
             Path(f"{ledger}-wal"): b"a stale write-ahead log left by a crash" * 50,
             Path(f"{ledger}-journal"): b"a stale rollback journal left by a crash" * 50}
    for path, content in files.items():
        path.write_bytes(content)
    watch = Sqlite3Watch()
    monkeypatch.setattr(db, "sqlite3", watch)

    with pytest.raises(db.DatabaseCorrupt, match="has not modified it"):
        db.connect_existing(ledger)

    assert {path: path.read_bytes() for path in files} == files
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(path.name for path in files)
    assert watch.opened and all("mode=ro&immutable=1" in opened for opened in watch.opened)


def test_a_ledger_whose_check_fails_every_time_is_still_called_corrupt(ledger, monkeypatch):
    ledger.write_bytes(mid_write_copy_of(ledger.read_bytes()))
    torn = ledger.read_bytes()
    watch = Sqlite3Watch()
    monkeypatch.setattr(db, "sqlite3", watch)

    with pytest.raises(db.DatabaseCorrupt, match="failed integrity check"):
        db.connect_existing(ledger)

    assert ledger.read_bytes() == torn
    assert all("mode=ro&immutable=1" in opened for opened in watch.opened)
