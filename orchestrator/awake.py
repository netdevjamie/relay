"""Keeping the Mac awake while a phase runs, and noticing when it slept anyway.

An agent that should take minutes can report hours later because the laptop
went to sleep. Relay cannot stop a closed lid from sleeping the machine, but
it can do two things: hold the machine awake itself for as long as a phase
is expected to run (macOS `caffeinate`), and say plainly, when the step's
verdict is picked up, that the machine slept between the step being
dispatched and its artifact being written (from the system's power log).

Does nothing on a system without `caffeinate`. Imports no other Relay module.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SECONDS = 2 * 60 * 60
# One holder per task, remembered in the task folder so the next command
# (a separate process) can release it.
PID_FILE = ".awake.pid"


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("caffeinate") is not None


def hold(task_dir: Path, seconds: int) -> bool:
    """Keep the machine from idle-sleeping for up to `seconds`, on behalf of
    one task. Replaces any earlier hold for the task. Returns True when a
    hold was started."""
    release(task_dir)
    if seconds <= 0 or not available() or not task_dir.is_dir():
        return False
    # -i: no idle sleep. -s: no sleep at all while on mains power. -t: let go
    # by itself after this long, so a forgotten hold cannot last for ever.
    # Python: start_new_session=True detaches it, so it outlives this command.
    holder = subprocess.Popen(["caffeinate", "-i", "-s", "-t", str(seconds)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    (task_dir / PID_FILE).write_text(f"{holder.pid}\n")
    return True


def release(task_dir: Path) -> None:
    """Let go of the task's hold, if it has one. Safe to call at any time."""
    pid_file = task_dir / PID_FILE
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    pid_file.unlink(missing_ok=True)
    # The number may have been reused by another program since: only stop it
    # if it is still a caffeinate.
    if _command_of(pid) == "caffeinate":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _command_of(pid: int) -> str | None:
    try:
        shown = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True)
    except OSError:
        return None
    return Path(shown.stdout.strip()).name or None


# Lines of `pmset -g log` that mark a change of power state, e.g.
#   2026-10-05 06:25:21 -0400 Sleep      \tEntering Sleep state due to ...
#   2026-10-05 06:37:28 -0400 Wake       \tWake from Deep Idle ...
# Between a Sleep and the next full Wake the machine may stir ("DarkWake")
# for its own housekeeping; nothing of ours runs then, so it counts as asleep.
_POWER_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) (Sleep|DarkWake|Wake)\s*\t")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def sleeps_between(started: datetime, finished: datetime) -> list[dict] | None:
    """Each stretch the machine was asleep that began from `started` to
    `finished` (both with a time zone), oldest first: when it went to sleep,
    when it next fully woke, and the seconds between.

    Read from the system's own power log, which keeps about a week. None
    when the log cannot be read (not macOS, or the command failed): unknown
    is not the same as "did not sleep"."""
    if sys.platform != "darwin":
        return None
    try:
        shown = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if shown.returncode != 0:
        return None
    return _asleep_stretches(shown.stdout, started, finished)


def _asleep_stretches(power_log: str, started: datetime, finished: datetime) -> list[dict]:
    stretches = []
    asleep_since = None
    for line in power_log.splitlines():
        found = _POWER_LINE.match(line)
        if not found:
            continue
        when = datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S %z")
        kind = found.group(2)
        if kind == "Sleep" and asleep_since is None:
            asleep_since = when
        elif kind == "Wake" and asleep_since is not None:
            if started <= asleep_since <= finished:
                stretches.append({"slept_at": asleep_since.astimezone(timezone.utc).strftime(_STAMP),
                                  "woke_at": when.astimezone(timezone.utc).strftime(_STAMP),
                                  "asleep_seconds": int((when - asleep_since).total_seconds())})
            asleep_since = None
    return stretches
