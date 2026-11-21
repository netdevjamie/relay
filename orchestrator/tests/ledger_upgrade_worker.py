"""One of the two processes in test_ledger_upgrade.py's race: it adds a
throwaway upgrade step 2 (one row into a probe table), waits until the
barrier file exists, then opens the ledger for writing.

    python3 ledger_upgrade_worker.py <ledger> <barrier>

Exits 0 when the open succeeded. Run only against a temp ledger the test
made.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from settings import VALID_PHASES, VALID_STATUSES, VALID_VERDICTS


def _add_a_probe_row(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS upgrade_probe (applied INTEGER)")
    conn.execute("INSERT INTO upgrade_probe VALUES (2)")


def main(ledger: Path, barrier: Path) -> int:
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    db._UPGRADES.append(db.Upgrade(2, _add_a_probe_row, readable_before=True))
    deadline = time.monotonic() + 30
    while not barrier.exists():
        if time.monotonic() > deadline:
            return 2
        time.sleep(0.001)
    db.connect_existing(ledger).close()
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
