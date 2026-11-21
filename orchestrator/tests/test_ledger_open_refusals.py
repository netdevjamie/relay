"""Opening the ledger where SQLite cannot make its side files.

A ledger in WAL mode needs relay.db-wal and relay.db-shm beside it. On
Linux both are removed when the last connection closes, so the next open
must create them. In a folder Relay cannot write, it cannot, and SQLite
refuses the open with its own error. Relay must say so in one line of its
own, with no traceback, for a read-write and a read-only open alike.

Every ledger here is built in a temp folder.
"""

import json
import os
from pathlib import Path

import pytest

import commands
import db
import settings
from settings import DEFAULT_CONFIG, VALID_PHASES, VALID_STATUSES, VALID_VERDICTS
from tests.cli_edge import run_relay

SIDE_FILES = ("relay.db-wal", "relay.db-shm")


@pytest.fixture(autouse=True)
def enums():
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)


@pytest.fixture
def ledger_in_a_locked_folder(tmp_path):
    """A healthy current ledger, closed, with no side files, in a folder
    set to 555. The folder is opened up again after the test."""
    folder = tmp_path / "locked"
    folder.mkdir()
    ledger = folder / "relay.db"
    db.init_database(ledger).close()
    for name in SIDE_FILES:
        (folder / name).unlink(missing_ok=True)
    folder.chmod(0o555)
    yield ledger
    folder.chmod(0o755)


@pytest.fixture
def ledger_unchanged(ledger_in_a_locked_folder):
    """Checks after the test that the ledger's bytes are as they were and
    no file was added beside it."""
    ledger = ledger_in_a_locked_folder
    before = ledger.read_bytes()
    yield ledger
    assert ledger.read_bytes() == before
    assert sorted(path.name for path in ledger.parent.iterdir()) == ["relay.db"]


def _skip_when_the_folder_is_writable_anyway(folder: Path) -> None:
    if os.access(folder, os.W_OK):
        pytest.skip("this user can write a 555 folder (root), so nothing is refused")


@pytest.mark.parametrize("read_only", [False, True], ids=["read-write", "read-only"])
def test_an_open_where_the_side_files_cannot_be_made_raises_relays_refusal(
        ledger_unchanged, read_only):
    ledger = ledger_unchanged
    _skip_when_the_folder_is_writable_anyway(ledger.parent)
    with pytest.raises(db.DatabaseUnavailable) as refusal:
        db.connect_existing(ledger, read_only=read_only)
    message = str(refusal.value)
    assert str(ledger.parent) in message
    assert "Nothing was changed" in message
    assert "\n" not in message


def test_relay_list_in_that_folder_prints_one_line_and_no_traceback(
        ledger_unchanged, monkeypatch, capsys):
    ledger = ledger_unchanged
    _skip_when_the_folder_is_writable_anyway(ledger.parent)
    monkeypatch.setattr(settings, "_db_connection", None)
    monkeypatch.setenv("RELAY_DB_PATH", str(ledger))
    with pytest.raises(SystemExit) as exit_:
        run_relay(dict(DEFAULT_CONFIG, persistence_backend="sqlite"), "list")
    assert exit_.value.code == 1
    printed = capsys.readouterr()
    lines = printed.out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"Error: Relay could not open the ledger at {ledger}")
    assert "Traceback" not in printed.out + printed.err


def test_relay_list_json_in_that_folder_answers_ledger_unavailable(
        ledger_unchanged, monkeypatch, capsys):
    ledger = ledger_unchanged
    _skip_when_the_folder_is_writable_anyway(ledger.parent)
    monkeypatch.setattr(settings, "_db_connection", None)
    monkeypatch.setenv("RELAY_DB_PATH", str(ledger))
    config = dict(DEFAULT_CONFIG, persistence_backend="sqlite")
    assert commands.run_json_command("list", None, config) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["error"]["code"] == "ledger_unavailable"
