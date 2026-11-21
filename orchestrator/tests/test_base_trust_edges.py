"""Inputs the first base-trust tests did not try.

- The files backend's whole record: start writes the base commit into
  run.yaml, a later step moves it forward, a rewrite or a forged value
  never moves it the wrong way.
- The remote is asked with no terminal, even when Relay has one.
- Remote listings git passes on as the remote gave them: a null id, or the
  id of something that is not a commit. Each comes from a throwaway remote
  helper (`git-remote-fake`) on PATH, so no network is used.

Every repo is a throwaway under tmp_path.
"""

import os
import pty
import stat

import pytest

import base_trust
import db
import run_state
import settings
from tests.git_fixtures import git
from tests.test_base_trust import (advance, base_events, finish_the_current_step, launched,  # noqa: F401
                                   next_, remote, start)
from tests.test_send_back import config  # noqa: F401  (a fixture)


# ---------------------------------------------------------------------------
# The files backend
# ---------------------------------------------------------------------------

@pytest.fixture
def files_config(config) -> dict:
    return dict(config, persistence_backend="files")


def designer_done(task_id: str = "t") -> None:
    """Play the Designer, whose report is the first step's."""
    (settings.TASKS_DIR / task_id / "01-designer.md").write_text(
        f"---\ntask_id: {task_id}\nphase: designer\nstep: 1\nverdict: ready-for-architect\n---\n\nBody.\n")


def run_yaml(task_id: str = "t") -> str:
    return run_state.run_state_path(task_id).read_text()


def test_the_files_backend_records_the_base_commit_and_moves_it_forward(remote, files_config, launched,
                                                                       capsys):
    assert start(remote, files_config) == 0
    first = git(remote.upstream, "rev-parse", "main")
    state = run_state.load_run_state("t", files_config)
    assert (state["base"], state["base_ref"], state["base_commit"]) == (
        "origin/main", "refs/remotes/origin/main", first)
    newer = advance(remote.upstream)
    git(remote.clone, "fetch", "-q", "origin")
    designer_done()
    code, out = next_(capsys, files_config)
    assert code == 0
    assert f"Base origin/main moved to {newer[:7]}.\n" in out
    assert run_state.load_run_state("t", files_config)["base_commit"] == newer
    assert launched == ["designer", "architect"]


def test_the_files_backend_refuses_a_rewritten_base_and_leaves_run_yaml_as_it_was(
        remote, files_config, launched, capsys):
    assert start(remote, files_config) == 0
    recorded = git(remote.upstream, "rev-parse", "main")
    git(remote.upstream, "commit", "-q", "--amend", "-m", "the base, rewritten")
    git(remote.clone, "fetch", "-q", "origin")
    designer_done()
    before = run_yaml()
    code, out = next_(capsys, files_config)
    assert code == 1
    assert f"Error: the base origin/main no longer contains {recorded[:7]}," in out
    assert run_yaml() == before
    assert launched == ["designer"]


@pytest.mark.parametrize("forged", ["main", "origin/main", "1" * 39, ["1" * 40]])
def test_a_base_commit_in_run_yaml_that_is_no_commit_id_is_confirmed_again(
        forged, remote, files_config, launched, capsys):
    assert start(remote, files_config) == 0
    state = run_state.load_run_state("t", files_config)
    state["base_commit"] = forged
    run_state.save_run_state("t", state, files_config)
    designer_done()
    code, _ = next_(capsys, files_config)
    assert code == 0
    assert run_state.load_run_state("t", files_config)["base_commit"] == git(remote.upstream, "rev-parse", "main")


# ---------------------------------------------------------------------------
# What the remote lists
# ---------------------------------------------------------------------------

HELPER = """#!/bin/sh
while read line; do
  case "$line" in
    capabilities) printf 'list\\n\\n' ;;
    list*) cat '{listing}'; printf '\\n' ;;
    *) exit 0 ;;
  esac
done
"""


@pytest.fixture
def listed(remote, tmp_path, monkeypatch):
    """Point the clone's origin at a remote helper that lists exactly the
    lines written into the returned file (`<id> <ref>` each)."""
    listing = tmp_path / "listing"
    listing.write_text("")
    helper = tmp_path / "bin" / "git-remote-fake"
    helper.parent.mkdir()
    helper.write_text(HELPER.format(listing=listing))
    helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{helper.parent}{os.pathsep}{os.environ['PATH']}")
    return listing


def point_origin_at_the_helper(remote) -> None:
    git(remote.clone, "remote", "set-url", "origin", "fake::listing")


@pytest.mark.parametrize("what", ["null id", "a tree's id"])
def test_a_listed_id_that_is_no_commit_here_refuses_start(what, remote, listed, config, launched, capsys):
    oid = "0" * 40 if what == "null id" else git(remote.clone, "rev-parse", "HEAD^{tree}")
    listed.write_text(f"{oid} refs/heads/main\n")
    point_origin_at_the_helper(remote)
    capsys.readouterr()
    assert start(remote, config) == 1
    assert f"Error: origin's main is at {oid[:7]}, which this repo does not have yet; fetch and start again." in (
        capsys.readouterr().out)
    assert run_state.load_run_state("t", config) is None
    assert db.get_events(settings.get_db_connection(), "t") == []


@pytest.mark.parametrize("what", ["null id", "a tree's id"])
def test_a_listed_id_that_is_no_commit_here_keeps_the_recorded_commit_at_a_step(
        what, remote, listed, config, launched, capsys):
    assert start(remote, config) == 0
    recorded = base_events()[-1]["commit"]
    oid = "0" * 40 if what == "null id" else git(remote.clone, "rev-parse", "HEAD^{tree}")
    listed.write_text(f"{oid} refs/heads/main\n")
    point_origin_at_the_helper(remote)
    finish_the_current_step("t", "ready-for-architect", config)
    code, out = next_(capsys, config)
    assert code == 0
    assert f"Base not moved (origin's main is at {oid[:7]}," in out
    assert f"using {recorded[:7]}.\n" in out
    assert [event["commit"] for event in base_events()] == [recorded]
    assert launched == ["designer", "architect"]


# ---------------------------------------------------------------------------
# No terminal for the remote, even when Relay has one
# ---------------------------------------------------------------------------

def test_the_remote_gets_no_terminal_even_when_relay_has_one(tmp_path):
    """The owner runs Relay in a terminal; ssh asking for a passphrase opens
    /dev/tty. The ask runs in a session of its own, so it has none. This
    runs the ask in a child that pty.fork gave a terminal, so it can fail
    wherever the suite runs, with or without one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    marker = tmp_path / "had-a-terminal"
    script = tmp_path / "fake-ssh"
    script.write_text(f"#!/bin/sh\n( exec 3</dev/tty ) 2>/dev/null && touch '{marker}'\nexit 255\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    git(repo, "remote", "add", "origin", "ssh://example.invalid/r")
    git(repo, "config", "core.sshCommand", str(script))
    pid, terminal = pty.fork()
    if pid == 0:   # the child: Relay with a terminal of its own
        code = 3
        try:
            os.close(os.open("/dev/tty", os.O_RDONLY))
            code = 0 if base_trust.ls_remote_heads(repo, "origin") is None else 4
        finally:
            os._exit(code)
    while True:
        try:
            if not os.read(terminal, 1024):
                break
        except OSError:
            break
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, "the child had no terminal, or the remote answered"
    assert not marker.exists(), "the remote's ssh opened the owner's terminal"
