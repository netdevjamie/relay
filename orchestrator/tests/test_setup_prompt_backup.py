"""setup.sh backs up a file of the user's own before a role prompt replaces
it, and never an older Relay prompt. Runs the script's is_relay_prompt
function on stand-in files."""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SETUP = (REPO / "setup.sh").read_text()


def is_relay_prompt(file: Path, relay_home: Path) -> bool:
    function = re.search(r"^is_relay_prompt\(\) \{\n.*?^\}\n", SETUP, re.S | re.M)
    assert function, "setup.sh no longer defines is_relay_prompt"
    script = f'set -euo pipefail\n{function.group(0)}\nis_relay_prompt "$1"'
    result = subprocess.run(["bash", "-c", script, "setup", str(file)], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                 "SCRIPT_DIR": str(REPO), "RELAY_HOME": str(relay_home)})
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.fixture
def places(tmp_path):
    relay_home, agents = tmp_path / "relay", tmp_path / "claude-agents"
    (relay_home / "agents").mkdir(parents=True)
    agents.mkdir()
    return relay_home, agents


def older_builder_prompt() -> str:
    commits = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H", "--", "agents/builder.md"],
                             capture_output=True, text=True, check=True).stdout.split()
    if len(commits) < 2:
        pytest.skip("needs a checkout with the prompt's history")
    return subprocess.run(["git", "-C", str(REPO), "show", f"{commits[1]}:agents/builder.md"],
                          capture_output=True, text=True, check=True).stdout


def test_the_last_installs_copy_is_relays(places):
    relay_home, agents = places
    (relay_home / "agents" / "builder.md").write_text("what the last install put there\n")
    (agents / "builder.md").write_text("what the last install put there\n")
    assert is_relay_prompt(agents / "builder.md", relay_home)


def test_an_older_version_from_the_history_is_relays(places):
    relay_home, agents = places
    (agents / "builder.md").write_text(older_builder_prompt())
    assert is_relay_prompt(agents / "builder.md", relay_home)


def test_a_file_of_the_users_own_is_not(places):
    relay_home, agents = places
    (relay_home / "agents" / "builder.md").write_text("Relay's prompt\n")
    (agents / "builder.md").write_text("---\nname: Builder\n---\nMy own builder.\n")
    assert not is_relay_prompt(agents / "builder.md", relay_home)
