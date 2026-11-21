"""The process test_ledger_upgrade.py kills in the middle of a backup: it
opens a behind ledger for writing, and once the backup copy is half
written it kills itself with SIGKILL, as a power cut or `kill -9` would.

    python3 ledger_backup_crash_worker.py <ledger>

Never returns normally. Run only against a temp ledger the test made.
"""

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from settings import VALID_PHASES, VALID_STATUSES, VALID_VERDICTS


def _write_half_then_die(source, copy, *args, **kwargs) -> None:
    data = source.read()
    copy.write(data[:len(data) // 2])
    copy.flush()
    os.fsync(copy.fileno())
    os.kill(os.getpid(), signal.SIGKILL)


def main(ledger: Path) -> int:
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    db.shutil.copyfileobj = _write_half_then_die
    db.connect_existing(ledger).close()
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
