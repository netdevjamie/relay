"""The commands DEPLOYMENT.md gives for an install, run on a stand-in home.

The backup, the rollback, the comparison after an install and the smoke are
read out of the notes and run as written, with HOME set to a folder the test
makes. The stand-in install is made by setup.sh's own `mkdir` and `cp`
lines, so a place the install starts to write and the rollback does not put
back fails here. Nothing under the real home folder is read or written.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL_NOTES = REPO / "DEPLOYMENT.md"
PROFILE = REPO / ".relay" / "profile.yaml"
SETUP_SCRIPT = REPO / "setup.sh"
ROLLBACK_HEADING = "Backup and rollback"
# setup.sh copies this only when missing: the example config when the
# install has no config.yaml.
COPIES_AN_INSTALL_DOES_NOT_ALWAYS_MAKE = ("config.example.yaml",)
INSTALL_WRITE = re.compile(r'^\s*(mkdir -p|cp) "\$\{(SCRIPT_DIR|RELAY_HOME|HOME)\}')
# What an install must leave as it is, and a rollback after it.
LEDGER = "Projects/.relay/relay.db"
TASK_REPORT = "Projects/.relay/tasks/some-task/01-designer.md"
INSTALL_CONFIG = "Projects/.relay/orchestrator/config.yaml"
REGISTRY = "Projects/.relay/registry.yaml"
NEVER_IN_A_BACKUP = {"relay.db", "tasks", "locks", "config.yaml", "registry.yaml"}


def fenced_blocks() -> list[list[str]]:
    """Every fenced block of the notes, as its lines without indentation."""
    blocks, current = [], None
    for line in INSTALL_NOTES.read_text().splitlines():
        if line.strip() == "```":
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
        elif current is not None:
            current.append(line.strip())
    return blocks


def the_block(description: str, is_wanted) -> str:
    wanted = [block for block in fenced_blocks() if block and is_wanted(block)]
    assert len(wanted) == 1, f"DEPLOYMENT.md must give {description} once, in a code block"
    return "\n".join(wanted[0])


def backup_block() -> str:
    return the_block("the backup before an install",
                     lambda block: block[0].startswith("BK="))


def restore_block() -> str:
    return the_block("the rollback's copy lines",
                     lambda block: all(line.startswith('cp ') and ' "$BK"/' in line
                                       for line in block))


def comparison_block() -> str:
    return the_block("the comparison after an install",
                     lambda block: block[0].startswith("diff -rq agents ~/.claude/agents"))


def run_in(home: Path, script: str, stop_at_first_error: bool = True, **more_environment):
    """Run shell lines with HOME set to the stand-in, from the repo root."""
    if stop_at_first_error:
        script = "set -eu\n" + script
    environment = {"HOME": str(home), "PATH": os.environ["PATH"], **more_environment}
    return subprocess.run(["bash", "-c", script], cwd=REPO, env=environment,
                          capture_output=True, text=True, timeout=60)


def install_into(home: Path) -> None:
    """What `./setup.sh` copies, by its own lines, into the stand-in home."""
    writes = [line for line in SETUP_SCRIPT.read_text().splitlines()
              if INSTALL_WRITE.match(line)
              and not any(name in line for name in COPIES_AN_INSTALL_DOES_NOT_ALWAYS_MAKE)]
    assert any("cp " in line for line in writes), "setup.sh no longer copies with the lines read here"
    done = run_in(home, "\n".join(writes), SCRIPT_DIR=str(REPO),
                  RELAY_HOME=str(home / "Projects" / ".relay"))
    assert done.returncode == 0, done.stderr


def files_under(home: Path, apart_from: Path | None = None) -> dict[str, bytes]:
    return {str(path.relative_to(home)): path.read_bytes()
            for path in sorted(home.rglob("*"))
            if path.is_file() and not path.is_symlink()
            and (apart_from is None or apart_from not in path.parents)}


@pytest.fixture
def home(tmp_path):
    stand_in = tmp_path / "home"
    stand_in.mkdir()
    install_into(stand_in)
    return stand_in


@pytest.fixture
def rolled_back(home):
    """An older install is backed up, a newer one is installed over it, Relay
    records more work, and the rollback is run. Returns the files at each
    point, each as {path under the stand-in home: content}."""
    for path in home.rglob("*"):
        if path.is_file():
            path.write_bytes(b"older\n" + path.read_bytes())
    for kept, content in ((LEDGER, "ledger at the backup"), (TASK_REPORT, "a report"),
                          (INSTALL_CONFIG, "max_retries: 3"), (REGISTRY, "repos: {}")):
        (home / kept).parent.mkdir(parents=True, exist_ok=True)
        (home / kept).write_text(content)
    before = files_under(home)

    backed_up = run_in(home, backup_block())
    assert backed_up.returncode == 0, backed_up.stderr
    backup_folder = Path(backed_up.stdout.split()[-1])
    assert home in backup_folder.parents, "the backup block must print the folder it made last"

    install_into(home)
    installed = files_under(home, apart_from=backup_folder)
    written = sorted(path for path in installed if installed[path] != before.get(path))
    (home / LEDGER).write_text("ledger after more work")
    (home / TASK_REPORT).with_name("02-architect.md").write_text("a later report")

    restored = run_in(home, restore_block(), BK=str(backup_folder))
    assert restored.returncode == 0, restored.stderr
    return {"before": before, "written": written, "backup_folder": backup_folder,
            "after": files_under(home, apart_from=backup_folder)}


def test_the_rollback_puts_back_every_file_the_install_writes(rolled_back):
    before, after = rolled_back["before"], rolled_back["after"]
    assert ".claude/agents/builder.md" in rolled_back["written"]
    assert "Projects/.relay/orchestrator/agents.py" in rolled_back["written"]
    not_put_back = [path for path in rolled_back["written"] if after[path] != before.get(path)]
    assert not_put_back == [], (
        "the rollback in DEPLOYMENT.md leaves files the install wrote as the install left them")


def test_the_rollback_leaves_the_ledger_and_the_tasks_as_they_are_now(rolled_back):
    """A rollback never puts back the ledger or a task folder of the day of
    the backup: what Relay recorded since would be lost."""
    after = rolled_back["after"]
    later_report = str(Path(TASK_REPORT).with_name("02-architect.md"))
    assert after[LEDGER] == b"ledger after more work", "the rollback put back an older ledger"
    assert after.get(later_report) == b"a later report"
    assert after[INSTALL_CONFIG] == b"max_retries: 3" and after[REGISTRY] == b"repos: {}"


def test_the_backup_holds_only_what_the_install_writes(rolled_back):
    backup_folder = rolled_back["backup_folder"]
    held = {path.name for path in backup_folder.rglob("*")}
    assert held & NEVER_IN_A_BACKUP == set(), (
        "the backup in DEPLOYMENT.md holds the ledger, tasks or settings, which no rollback may restore")
    assert {"agents.py", "requirements.txt", "builder.md", "task-brief.md",
            "relay"} <= held


def test_the_profile_rollback_names_the_steps_and_what_never_to_restore():
    rollback = [line.split("rollback:", 1)[1] for line in PROFILE.read_text().splitlines()
                if line.strip().startswith("rollback:")]
    assert len(rollback) == 1, "the profile must name one rollback"
    assert f"### {ROLLBACK_HEADING}" in INSTALL_NOTES.read_text().splitlines(), (
        "DEPLOYMENT.md has no section with the rollback's steps")
    assert ROLLBACK_HEADING in rollback[0] and "DEPLOYMENT.md" in rollback[0], (
        "the profile's rollback does not name the section of DEPLOYMENT.md that has the steps")
    assert "Never restore relay.db or tasks/" in rollback[0], (
        "the profile's rollback does not say never to restore the ledger or the tasks")


def test_the_comparison_after_an_install_names_a_changed_file_in_each_place(home):
    """Nothing is printed for an install that equals the checkout, a leftover
    module included, and a changed file is named in each of the three places."""
    (home / "Projects" / ".relay" / "orchestrator" / "flow.py").write_text("# left over\n")
    comparison = comparison_block()
    same = run_in(home, comparison, stop_at_first_error=False)
    assert same.stdout == "" and same.stderr == "", (
        "the comparison in DEPLOYMENT.md prints a line for an install that equals the checkout")
    for changed in (home / ".claude" / "agents" / "security.md",
                    home / "Projects" / ".relay" / "agents" / "security.md",
                    home / "Projects" / ".relay" / "orchestrator" / "agents.py"):
        as_installed = changed.read_bytes()
        changed.write_bytes(as_installed + b"\nchanged\n")
        differs = run_in(home, comparison, stop_at_first_error=False)
        changed.write_bytes(as_installed)
        assert str(changed) in differs.stdout, (
            f"the comparison in DEPLOYMENT.md does not name a changed {changed.relative_to(home)}")

