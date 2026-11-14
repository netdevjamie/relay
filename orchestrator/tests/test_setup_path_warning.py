"""setup.sh warns, without failing, when typing `relay` would run some other
command than the link it made in ~/.local/bin, and says which one runs. Runs
the script's warn_about_other_relay_on_path function with stand-in PATHs."""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SETUP = (REPO / "setup.sh").read_text()
SYSTEM_PATH = "/usr/bin:/bin"


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho stand-in\n")
    path.chmod(0o755)
    return path


def relay_install_and_link(tmp_path: Path) -> Path:
    """A stand-in installed wrapper and the ~/.local/bin link to it."""
    wrapper = executable(tmp_path / "install" / "relay")
    link = tmp_path / "home" / ".local" / "bin" / "relay"
    link.parent.mkdir(parents=True)
    link.symlink_to(wrapper)
    return link


def warn(link: Path, path: str) -> subprocess.CompletedProcess:
    function = re.search(r"^warn_about_other_relay_on_path\(\) \{\n.*?^\}\n", SETUP, re.S | re.M)
    assert function, "setup.sh no longer defines warn_about_other_relay_on_path"
    script = f'set -euo pipefail\n{function.group(0)}\nwarn_about_other_relay_on_path "$1"'
    return subprocess.run(["bash", "-c", script, "setup", str(link)],
                          capture_output=True, text=True, env={"PATH": path})


def test_setup_runs_the_check_right_after_it_makes_the_link():
    link_line = 'ln -sf "${RELAY_HOME}/relay" "${BIN_DIR}/relay"\n'
    assert link_line + 'warn_about_other_relay_on_path "${BIN_DIR}/relay"\n' in SETUP


def test_no_warning_when_the_link_is_what_relay_runs(tmp_path):
    link = relay_install_and_link(tmp_path)
    executable(tmp_path / "later" / "relay")
    done = warn(link, f"{link.parent}:{tmp_path / 'later'}:{SYSTEM_PATH}")
    assert done.returncode == 0 and done.stderr == ""


def test_no_warning_when_no_relay_is_on_the_path(tmp_path):
    link = relay_install_and_link(tmp_path)
    done = warn(link, SYSTEM_PATH)
    assert done.returncode == 0 and done.stderr == ""


def test_an_earlier_relay_on_the_path_is_named_as_the_one_that_runs(tmp_path):
    link = relay_install_and_link(tmp_path)
    other = executable(tmp_path / "other tool" / "relay")
    done = warn(link, f"{other.parent}:{link.parent}:{SYSTEM_PATH}")
    assert done.returncode == 0
    assert f"another relay is on your PATH: {other}" in done.stderr
    assert f"It comes before {link.parent}" in done.stderr
    assert str(link) in done.stderr


def test_a_relay_elsewhere_when_the_bin_folder_is_off_the_path_is_named(tmp_path):
    link = relay_install_and_link(tmp_path)
    other = executable(tmp_path / "other tool" / "relay")
    done = warn(link, f"{other.parent}:{SYSTEM_PATH}")
    assert done.returncode == 0
    assert f"another relay is on your PATH: {other}" in done.stderr
    assert f"{link.parent} is not on your PATH" in done.stderr


def test_another_link_to_the_same_install_is_not_a_conflict(tmp_path):
    link = relay_install_and_link(tmp_path)
    second = tmp_path / "second" / "relay"
    second.parent.mkdir()
    second.symlink_to(link.resolve())
    done = warn(link, f"{second.parent}:{link.parent}:{SYSTEM_PATH}")
    assert done.returncode == 0 and done.stderr == ""
