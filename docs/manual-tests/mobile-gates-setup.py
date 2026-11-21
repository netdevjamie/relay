"""Scratch Relay home for the phone test plan (mobile-gates.md).

    python3 docs/manual-tests/mobile-gates-setup.py          make it
    python3 docs/manual-tests/mobile-gates-setup.py --clean  remove it

It makes $TMPDIR/relay-phone-gates/ with its own ledger and tasks
folder, never the live ~/Projects/.relay, and walks two scratch tasks with
the suite's own helpers:
  phone-review  waiting at the Reviewer's gate (step 4)
  phone-close   waiting at Security's gate, which closes (step 6)
It also writes `relay` in that folder: this checkout's relay.py run against
the scratch home, so the plan's commands never touch the real install.
"""

import copy
import os
import shutil
import signal
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ORCH = REPO / "orchestrator"
HOME = Path(os.environ.get("TMPDIR", "/tmp")).resolve() / "relay-phone-gates"
BRIEF = ("# Phone test: {title}\n\nA scratch task for the phone test plan. Nothing here is real.\n\n"
         "Last line of the phone test brief.\n")


def release_holds() -> None:
    for pid_file in HOME.rglob(".awake.pid"):
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def clean() -> None:
    if HOME.exists():
        release_holds()
        shutil.rmtree(HOME)
    print(f"Removed {HOME}" if not HOME.exists() else f"Could not remove {HOME}")


def make() -> None:
    if HOME.exists():
        sys.exit(f"{HOME} already exists. Run with --clean first.")
    (HOME / "home").mkdir(parents=True)
    (HOME / "wt").mkdir()
    os.environ.update(RELAY_HOME=str(HOME / "home"), RELAY_DB_PATH=str(HOME / "home" / "relay.db"),
                      RELAY_WORKTREES_ROOT=str(HOME / "wt"))
    sys.path.insert(0, str(ORCH))
    os.chdir(ORCH)
    import settings
    for path in (settings.TASKS_DIR, settings._db_path()):
        assert str(path).startswith(str(HOME)), f"not the scratch home: {path}"
    import awake
    import db
    from settings import DEFAULT_CONFIG, VALID_PHASES, VALID_STATUSES, VALID_VERDICTS
    settings.TASKS_DIR.mkdir(parents=True)
    db.configure(VALID_PHASES, VALID_VERDICTS, VALID_STATUSES)
    db.init_database(settings._db_path()).close()
    # The walk plays the agents with one-word reports, as the suite does.
    awake.hold = lambda *args, **kwargs: False
    import commands
    from tests import test_gate_card as walks
    from tests import test_send_back
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["persistence_backend"] = "sqlite"
    config["human_gates"] = {name: False for name in DEFAULT_CONFIG["human_gates"]}
    for task_id, title, walk in (("phone-review", "review gate", walks.at_approval),
                                 ("phone-close", "security gate", walks.at_accept_close)):
        original = test_send_back.task_at

        def task_at(task_id, config, verdicts, title=title, original=original):
            (settings.TASKS_DIR / task_id).mkdir(parents=True)
            (settings.TASKS_DIR / task_id / "brief.md").write_text(BRIEF.format(title=title))
            commands.cmd_start(task_id, auto=False, config=config)
            for verdict, extra in verdicts:
                test_send_back.finish_step(task_id, config, verdict, extra)
                commands.cmd_next(task_id, auto=False, config=config)

        walks.task_at = task_at
        walk(task_id, config)
        walks.task_at = original
    settings.CONFIG_PATH.parent.mkdir(parents=True)
    settings.CONFIG_PATH.write_text("persistence_backend: sqlite\nhuman_gates:\n" + "".join(
        f"  {name}: false\n" for name in DEFAULT_CONFIG["human_gates"]))
    wrapper = HOME / "relay"
    wrapper.write_text(
        "#!/bin/bash\n"
        f"export RELAY_HOME='{HOME}/home' RELAY_DB_PATH='{HOME}/home/relay.db' "
        f"RELAY_WORKTREES_ROOT='{HOME}/wt'\n"
        f"exec python3 '{ORCH}/relay.py' \"$@\"\n")
    wrapper.chmod(0o755)
    release_holds()
    print(f"\nReady. Run the plan's commands as: {wrapper} <command>")


if __name__ == "__main__":
    clean() if sys.argv[1:] == ["--clean"] else make()
