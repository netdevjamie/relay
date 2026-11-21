"""A test run past its time limit is asked to stop before it is made to.

On a timeout Relay sends SIGTERM to the run's whole process group, gives it
a grace period to clean up, and sends SIGKILL only to what is still there.
"""

import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evidence
from tests.git_fixtures import commit_all, repo, write_profile  # noqa: F401  (repo is a fixture)

GRACE = 2


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setattr(evidence, "TERM_GRACE_SECONDS", GRACE)


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_gone(pid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.05)
    return False


def run_with_timeout(repo: Path, tmp_path: Path, command: str):
    write_profile(repo, command)
    commit_all(repo)
    return evidence.run_tests(repo, tmp_path / "log.txt", timeout_seconds=1)


def test_a_child_that_traps_term_gets_to_write_its_marker(repo, tmp_path):
    marker = tmp_path / "cleaned-up"
    run = run_with_timeout(repo, tmp_path, f"trap 'echo done > {marker}; exit 0' TERM; "
                                           "while true; do sleep 0.1; done")
    assert run.outcome == "timed_out"
    assert run.reason == "The test command was stopped after 1 seconds."
    assert marker.read_text() == "done\n"


def test_a_child_that_ignores_term_is_killed_after_the_grace(repo, tmp_path):
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    run = run_with_timeout(repo, tmp_path, f"trap '' TERM; echo $$ > {pid_file}; "
                                           "while true; do sleep 0.1; done")
    took = time.monotonic() - started
    assert run.outcome == "timed_out"
    assert took >= 1 + GRACE - 0.5
    assert wait_until_gone(int(pid_file.read_text()))


def test_a_grandchild_is_stopped_too(repo, tmp_path):
    pid_file = tmp_path / "grandchild"
    run = run_with_timeout(repo, tmp_path, f"sh -c 'echo $$ > {pid_file}; exec sleep 60' & "
                                           "while true; do sleep 0.1; done")
    assert run.outcome == "timed_out"
    assert wait_until_gone(int(pid_file.read_text()))


def test_a_group_that_exits_in_the_grace_gets_no_kill(repo, tmp_path, monkeypatch):
    sent = []
    real_killpg = os.killpg

    def recording_killpg(group, sent_signal):
        if sent_signal != 0:
            sent.append(sent_signal)
        return real_killpg(group, sent_signal)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    run = run_with_timeout(repo, tmp_path, "sleep 30")
    assert run.outcome == "timed_out"
    assert sent == [signal.SIGTERM]
