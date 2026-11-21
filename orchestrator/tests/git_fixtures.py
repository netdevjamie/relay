"""Small git repos for tests of Relay's own test run: a repo on `main`
with a profile, and helpers to change its profile and commit."""

import json
import subprocess
from pathlib import Path

import pytest

import base_trust
import run_state
import settings


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def write_profile(repo: Path, test_command: str, extra: str = "") -> None:
    (repo / ".relay").mkdir(exist_ok=True)
    (repo / ".relay" / "profile.yaml").write_text(
        f"schema_version: 1\ncommands:\n  test: {json.dumps(test_command)}\n{extra}")


def commit_of(repo: Path, ref: str = "refs/heads/main") -> str:
    """The full commit id `ref` names in `repo`: what a test hands a reader
    that takes the base commit Relay confirmed, in place of a branch name."""
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def commit_all(repo: Path, message: str = "work") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def build_in(task_id: str, config: dict, checkout: Path, base: str = "main",
             branch: str = "task") -> None:
    """Point a started task at `checkout` as `relay start --repo` does: the
    repo registered in the test's own registry, the checkout, branch and
    base recorded, and the base commit Relay confirms recorded with them."""
    registry = settings.REGISTRY_PATH
    assert registry.parent == settings.TASKS_DIR.parent, "the registry must be the test's own"
    repo = Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    registry.write_text(f"repos:\n  the-repo: {repo}\n")
    trusted = base_trust.resolve(repo, base)
    commit = base_trust.confirm_at_start(repo, trusted)
    state = run_state.load_run_state(task_id, config)
    state.update(repo="the-repo", worktree_path=str(checkout), base=trusted.name, branch=branch)
    with run_state.ledger_writes(task_id, config):
        run_state.save_run_state(task_id, state, config)
        base_trust.record(task_id, state, trusted, commit, "start", None, config)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A git repo on `main` with a profile whose tests pass."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text("reports/\n")
    write_profile(path, "echo all good")
    commit_all(path, "start")
    return path


# A small test runner for fixture repos. Each tests/test_*.txt file holds
# one test per line, "name: how", where how is pass, fail, skip, error, or
# "needs PATH" (passes only when PATH exists). It writes a JUnit report to
# reports/junit.xml and exits 1 when anything failed or errored. When
# RUNNER_CWD_LOG is set, it appends the folder it ran in to that file.
CASE_RUNNER = '''\
import glob
import os
import sys

if os.environ.get("RUNNER_CWD_LOG"):
    with open(os.environ["RUNNER_CWD_LOG"], "a") as log:
        log.write(os.getcwd() + "\\n")
cases, bad, skipped = [], 0, 0
for path in sorted(glob.glob("tests/test_*.txt")):
    classname = path[:-4].replace("/", ".")
    for line in open(path).read().splitlines():
        if not line.strip():
            continue
        name, how = (part.strip() for part in line.split(":", 1))
        if how.startswith("needs "):
            how = "pass" if os.path.exists(how[6:]) else "fail"
        inner = {"pass": "", "fail": '<failure message="assert"/>',
                 "error": '<error message="ImportError: no feature"/>', "skip": "<skipped/>"}[how]
        bad += how in ("fail", "error")
        skipped += how == "skip"
        cases.append(f'<testcase classname="{classname}" name="{name}">{inner}</testcase>')
os.makedirs("reports", exist_ok=True)
failures = sum("<failure" in case for case in cases)
errors = sum("<error" in case for case in cases)
with open("reports/junit.xml", "w") as report:
    report.write(f'<testsuite tests="{len(cases)}" failures="{failures}" errors="{errors}" '
                 f'skipped="{skipped}">' + "".join(cases) + "</testsuite>")
sys.exit(1 if bad else 0)
'''


def write_cases(repo: Path, name: str, cases: dict[str, str]) -> None:
    """Write tests/<name>.txt for CASE_RUNNER: {test name: how}."""
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / f"{name}.txt").write_text("".join(f"{test}: {how}\n" for test, how in cases.items()))


@pytest.fixture
def cases_repo(tmp_path) -> Path:
    """A git repo on `main` whose profile runs CASE_RUNNER and names its
    report, with three passing tests in tests/test_a.txt."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text("reports/\n")
    (path / "runner.py").write_text(CASE_RUNNER)
    write_profile(path, "python3 runner.py", "test_report: reports/junit.xml\n")
    write_cases(path, "test_a", {"test_one": "pass", "test_two": "pass", "test_three": "pass"})
    commit_all(path, "start")
    return path
