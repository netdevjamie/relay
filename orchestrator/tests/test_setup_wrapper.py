"""The `relay` wrapper setup.sh installs runs the install it sits in when
RELAY_HOME is not set, and the one RELAY_HOME names when it is. Runs the
script's point_wrapper_at_install function on stand-in installs."""

import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SETUP = (REPO / "setup.sh").read_text()
PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"


def stand_in_install(folder: Path) -> Path:
    """An install whose engine prints the folder it was run from and the
    RELAY_HOME it was given, which is where the engine looks for its
    ledger, config and tasks (settings.py)."""
    (folder / "orchestrator").mkdir(parents=True)
    (folder / "orchestrator" / "relay.py").write_text(
        "import os, pathlib, sys\n"
        "print(pathlib.Path(sys.argv[0]).resolve().parents[1])\n"
        "print(pathlib.Path(os.environ['RELAY_HOME']).resolve())\n")
    shutil.copy(REPO / "relay", folder / "relay")
    return folder


def point_wrapper_at_install(install: Path) -> subprocess.CompletedProcess:
    function = re.search(r"^point_wrapper_at_install\(\) \{\n.*?^\}\n", SETUP, re.S | re.M)
    assert function, "setup.sh no longer defines point_wrapper_at_install"
    script = f'set -euo pipefail\n{function.group(0)}\npoint_wrapper_at_install "$1" "$2"'
    return subprocess.run(["bash", "-c", script, "setup", str(install / "relay"), str(install)],
                          capture_output=True, text=True, env={"PATH": PATH})


def run_relay(command: Path, home: Path, **environment) -> str:
    """The one install the engine was run from and told to use."""
    done = subprocess.run([str(command)], capture_output=True, text=True,
                          env={"PATH": PATH, "HOME": str(home), **environment})
    assert done.returncode == 0, done.stderr
    ran_from, told_to_use = done.stdout.splitlines()
    assert ran_from == told_to_use, "the wrapper ran one install's engine on another install"
    return ran_from


def test_setup_points_the_wrapper_it_installs_at_the_install():
    assert 'point_wrapper_at_install "${RELAY_HOME}/relay" "${RELAY_HOME}"' in SETUP


def test_the_linked_wrapper_runs_its_own_install_without_relay_home(tmp_path):
    install = stand_in_install(tmp_path / "an install's folder")
    default_install = stand_in_install(tmp_path / "home" / "Projects" / ".relay")
    assert point_wrapper_at_install(install).returncode == 0
    link = tmp_path / "bin" / "relay"
    link.parent.mkdir()
    link.symlink_to(install / "relay")
    assert run_relay(link, tmp_path / "home") == str(install.resolve())
    assert run_relay(link, tmp_path / "home",
                     RELAY_HOME=str(default_install)) == str(default_install.resolve())


def test_the_checkouts_own_wrapper_still_defaults_to_the_usual_folder(tmp_path):
    default_install = stand_in_install(tmp_path / "home" / "Projects" / ".relay")
    assert run_relay(REPO / "relay", tmp_path / "home") == str(default_install.resolve())


def test_a_wrapper_with_no_default_line_is_refused_and_left_as_it_was(tmp_path):
    install = stand_in_install(tmp_path / "install")
    (install / "relay").write_text("#!/usr/bin/env bash\necho changed\n")
    refused = point_wrapper_at_install(install)
    assert refused.returncode != 0 and "no default RELAY_HOME line" in refused.stderr
    assert (install / "relay").read_text() == "#!/usr/bin/env bash\necho changed\n"
    assert not (install / "relay.new").exists()
