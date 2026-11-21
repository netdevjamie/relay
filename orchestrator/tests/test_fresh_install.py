"""A fresh install keeps its tasks in the SQLite ledger from the first task.

setup.sh copies config.example.yaml to the install's config.yaml when the
install has none, so the example decides the backend of every new install.
An install that already has a config.yaml keeps it, and Relay's own
default stays the files backend, so upgrading never moves an existing
install to another backend.

The walk here is the quick start's, run through the real command line
(orchestrator/relay.py in a child process) with RELAY_HOME and HOME set to
a folder the test makes. Nothing under the real home folder is read or
written.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import db
import evidence
import settings
from tests.git_fixtures import commit_all, git, write_profile

ORCHESTRATOR = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ORCHESTRATOR / "config.example.yaml"
SETUP_SCRIPT = ORCHESTRATOR.parent / "setup.sh"
SETUP_COPIES_THE_EXAMPLE = (
    'cp "${SCRIPT_DIR}/orchestrator/config.example.yaml" "${RELAY_HOME}/orchestrator/config.yaml"')
TASK_ID = "first-task"


def test_the_example_config_keeps_tasks_in_the_sqlite_ledger(monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_PATH", EXAMPLE_CONFIG)
    assert settings.load_config()["persistence_backend"] == "sqlite"


def test_an_install_whose_config_names_no_backend_stays_on_the_files_backend(tmp_path, monkeypatch):
    """Every install made from the example before it named a backend has a
    config.yaml without the key. setup.sh never replaces that file, so
    Relay's own default is what keeps such an install on files."""
    older_config = tmp_path / "config.yaml"
    older_config.write_text("max_retries: 3\nauto_invoke: false\n")
    monkeypatch.setattr(settings, "CONFIG_PATH", older_config)
    assert settings.load_config()["persistence_backend"] == "files"


def fresh_install(relay_home: Path) -> None:
    """What setup.sh leaves that the commands read: the config copied from
    the example (setup.sh step 3), an empty registry (step 9) and the task
    folder (step 1)."""
    assert SETUP_COPIES_THE_EXAMPLE in SETUP_SCRIPT.read_text(), \
        "setup.sh no longer makes config.yaml from the example; this walk copies it as setup.sh did"
    (relay_home / "orchestrator").mkdir(parents=True)
    shutil.copy(EXAMPLE_CONFIG, relay_home / "orchestrator" / "config.yaml")
    (relay_home / "registry.yaml").write_text("repos: {}\n")
    (relay_home / "tasks").mkdir()


def relay(home: Path, relay_home: Path, cwd: Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = {"HOME": str(home), "RELAY_HOME": str(relay_home), "PATH": os.environ["PATH"],
                   "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(ORCHESTRATOR / "relay.py"), *arguments],
                          cwd=cwd, env=environment, capture_output=True, text=True, timeout=120)


def sample_repo(path: Path) -> Path:
    """A repo on `main` whose one unittest test passes."""
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    write_profile(path, "python3 -m unittest -q")
    (path / "test_sample.py").write_text(
        "import unittest\n\n\nclass Sample(unittest.TestCase):\n"
        "    def test_it_runs(self):\n        self.assertTrue(True)\n")
    commit_all(path, "Sample app")
    return path


def events_in(ledger: Path) -> list[dict]:
    connection = sqlite3.connect(str(ledger))
    connection.row_factory = sqlite3.Row
    try:
        return db.get_events(connection, TASK_ID)
    finally:
        connection.close()


def test_a_fresh_install_records_events_and_runs_the_tests_on_its_first_task(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    relay_home = home / "Projects" / ".relay"
    fresh_install(relay_home)
    repo = sample_repo(home / "sample")

    def run(*arguments: str) -> str:
        done = relay(home, relay_home, repo, *arguments)
        assert done.returncode == 0, f"relay {' '.join(arguments)}:\n{done.stdout}{done.stderr}"
        return done.stdout

    run("recover", "--init")
    ledger = relay_home / "relay.db"
    assert ledger.exists(), "relay recover --init made no ledger on a fresh install"

    run("register", str(repo), "--alias", "sample")
    commit_all(repo, "Register")
    git(repo, "switch", "-q", "-c", TASK_ID)
    task_folder = relay_home / "tasks" / TASK_ID
    task_folder.mkdir()
    (task_folder / "brief.md").write_text(
        "---\nsize: fix\nrisky: no\ndeploy: no\n---\n# Task Brief: a first task\n")
    run("start", TASK_ID, "--repo", "sample")
    assert db.EVENT_TASK_CREATED in [event["event_type"] for event in events_in(ledger)]

    (task_folder / "01-builder.md").write_text(
        f"---\ntask_id: {TASK_ID}\nphase: builder\nstep: 1\nverdict: ready-for-review\n---\n\nBuilt.\n")
    run("next", TASK_ID)
    test_runs = [event for event in events_in(ledger) if event["event_type"] == db.EVENT_TEST_RUN]
    assert [run["payload"]["outcome"] for run in test_runs] == [evidence.OUTCOME_PASSED]
