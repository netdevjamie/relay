"""Writes and checkpoints a ledger nonstop for a while, so that a reader
of the main file alone keeps meeting pages that are being copied in from
the WAL, the way another Relay process's writes look to it.

    python3 ledger_churn_worker.py <ledger> <seconds>

Run only against a temp ledger the test made.
"""

import sqlite3
import sys
import time


def churn(ledger: str, seconds: float) -> None:
    connection = sqlite3.connect(ledger, timeout=30)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS churn (filler BLOB)")
        connection.commit()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            connection.execute("INSERT INTO churn VALUES (randomblob(20000))")
            connection.execute("DELETE FROM churn WHERE rowid % 3 = 0")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


if __name__ == "__main__":
    churn(sys.argv[1], float(sys.argv[2]))
