"""Test evidence: Relay runs a repo's tests itself and keeps the result.

An agent saying "the tests pass" proves nothing. After the Builder and the
Tester finish, `relay next` runs the test command from the repo's profile
(`.relay/profile.yaml`) in the task's worktree and records the commit, the
exit code, how long it took and, when the profile names a JUnit report,
the counts. The roles and the routing act on that record, not on the
agent's description of it.

This module runs the command and reads the report. It never touches the
ledger: recorded_runs.py records what this returns.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import project_profile

# What a run can come to (the `outcome` of a recorded run).
OUTCOME_PASSED = "passed"                # the command exited 0
OUTCOME_FAILED = "failed"                # the command exited non-zero
OUTCOME_TIMED_OUT = "timed_out"          # the command ran past the time limit and was stopped
OUTCOME_UNCOMMITTED = "uncommitted"      # the worktree has changes that are not in a commit
OUTCOME_PROFILE_BROKEN = "profile_broken"  # the branch removed or broke the profile its base has
OUTCOME_NOT_RUN = "not_run"              # there was nothing Relay could run (no usable profile)

# Outcomes that mean the work is not ready, whatever the agent's verdict says.
FAILING_OUTCOMES = (OUTCOME_FAILED, OUTCOME_TIMED_OUT, OUTCOME_UNCOMMITTED, OUTCOME_PROFILE_BROKEN)

DEFAULT_TIMEOUT_SECONDS = 30 * 60
# The saved log keeps the end of the output, where test runners print
# failures and the summary.
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 50 * 1024 * 1024
# How much of the log a prompt quotes.
LOG_TAIL_LINES = 40
# What `relay next` prints, before the reason, when it asks the owner to
# confirm a test command and the base's profile was refused.
BASE_PROFILE_UNREAD = "The base branch's profile could not be read"


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=worktree, capture_output=True, text=True)


def head_commit(worktree: Path) -> str | None:
    """The commit the worktree is on, or None when it is not a git checkout."""
    result = _git(worktree, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def uncommitted_changes(worktree: Path) -> list[str]:
    """Paths that are changed or new and not ignored. A test run only proves
    something about a commit, so any of these makes the run meaningless."""
    result = _git(worktree, "status", "--porcelain")
    if result.returncode != 0:
        return []
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def base_profile(worktree: Path, base: str) -> tuple[object, str | None]:
    """(what the profile holds, why it was not read) for the profile at the
    point the task's branch left its base. The first is None when there is
    no profile there or Relay does not read it; the second is set, in one
    line, only when Relay refused it: larger than the size limit (its size
    is asked first), not UTF-8, or refused by bounded_yaml.

    The merge base is used, not the base branch's tip, so a profile change
    that landed on the base after the task started is not held against it."""
    fork = _git(worktree, "merge-base", base, "HEAD")
    start = fork.stdout.strip() if fork.returncode == 0 and fork.stdout.strip() else base
    at_start = f"{start}:{project_profile.PROFILE_RELATIVE_PATH.as_posix()}"
    sized = _git(worktree, "cat-file", "-s", at_start)
    if sized.returncode != 0:
        return None, None
    if not project_profile.fits_the_size_limit(sized.stdout):
        return None, f"it {project_profile.TOO_LARGE}."
    try:
        shown = _git(worktree, "show", at_start)
    except UnicodeDecodeError:
        return None, "it is not UTF-8."
    if shown.returncode != 0:
        return None, None
    try:
        return project_profile.profile_in(shown.stdout), None
    except project_profile.Unreadable as problem:
        # YAML's own errors take several lines; the reason is printed in one.
        return None, " ".join(problem.why.split())


def _test_setup_in(data) -> tuple[str | None, str | None]:
    """(test command, test report path) from what a profile holds."""
    if not isinstance(data, dict):
        return None, None
    commands = data.get("commands")
    command = commands.get("test") if isinstance(commands, dict) else None
    report = data.get("test_report")
    return (command if isinstance(command, str) else None,
            report if isinstance(report, str) else None)


def not_run(reason: str, commit: str | None = None) -> dict:
    """The record of a run Relay could not make."""
    return {"outcome": OUTCOME_NOT_RUN, "reason": reason, "commit": commit, "command": None,
            "test_report": None, "command_differs_from_base": False, "base_command": None,
            "base_profile_unread": None, "exit_code": None,
            "duration_seconds": None, "counts": None, "log": None}


def parse_junit_report(path: Path) -> dict | None:
    """Counts from a JUnit XML report: {"tests", "failures", "errors", "skipped"}.

    None when the file is missing, too big, not XML, or not a JUnit report.
    Accepts a single <testsuite> or a <testsuites> wrapper; nested suites
    are not counted twice."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
            return None
        content = path.read_bytes()
        # A JUnit report needs no document type or entities. Refusing them
        # closes off entity-expansion tricks whatever the XML library does.
        if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
            return None
        root = ElementTree.fromstring(content)
    except (OSError, ElementTree.ParseError):
        return None
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = root.findall("testsuite")
    else:
        return None
    if not suites:
        return None
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for name in counts:
            # JUnit writers disagree on the name of the skipped count.
            value = suite.get(name) or (suite.get("disabled") if name == "skipped" else None)
            try:
                counts[name] += int(value or 0)
            except ValueError:
                return None
    return counts


def _run_command(command: str, worktree: Path, log_path: Path, timeout_seconds: int) -> tuple[int | None, bool]:
    """Run one line of shell from the worktree, saving its output.
    Returns (exit code, timed out). The exit code is None on a timeout."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as log:
        # Python: start_new_session=True puts the shell and everything it
        #   starts in their own process group, so a timeout can stop them all.
        process = subprocess.Popen(["sh", "-c", command], cwd=worktree, stdout=log,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            exit_code = process.wait(timeout=timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            exit_code, timed_out = None, True
    _keep_end_of_log(log_path)
    return exit_code, timed_out


def _keep_end_of_log(log_path: Path) -> None:
    size = log_path.stat().st_size
    if size <= MAX_LOG_BYTES:
        return
    with open(log_path, "rb") as log:
        log.seek(size - MAX_LOG_BYTES)
        end = log.read()
    log_path.write_bytes(b"[earlier output dropped by Relay]\n" + end)


def log_tail(log_path: Path, lines: int = LOG_TAIL_LINES) -> str:
    """The last lines of a saved test log, for a prompt or a terminal."""
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def run_tests(worktree: Path, log_path: Path, *, base: str | None = None,
              timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run the profile's test command in `worktree` and describe what happened.

    The profile is read from the worktree, because a task may rightly change
    how its repo is tested. But the branch under test then decides what
    "the tests" are, so a command or report path that is not the base
    branch's is flagged (`command_differs_from_base`) and the owner confirms
    it before a pass is believed. Returns a plain dict ready to be stored:

        outcome, reason, commit, command, test_report, command_differs_from_base,
        base_command, base_profile_unread, exit_code, duration_seconds, counts, log

    `base_profile_unread` is why the base's profile was refused, when it was.
    """
    run = not_run("", commit=head_commit(worktree))
    # No base at all (a task with no branch of its own) leaves nothing to
    # compare with.
    base_data, run["base_profile_unread"] = base_profile(worktree, base) if base else (None, None)
    base_command, base_report = _test_setup_in(base_data)
    run["base_command"] = base_command

    report = project_profile.load_profile(worktree)
    if not report.valid:
        problem = "has no project profile" if not report.found else "has a project profile with errors"
        if base_command is not None:
            # The base could be tested and this branch can't: that is the
            # branch's doing, and it must not be a way to skip the tests.
            run["outcome"] = OUTCOME_PROFILE_BROKEN
            run["command_differs_from_base"] = True
            run["reason"] = (f"This branch {problem}, but its base has a test command "
                             f"({base_command}). Restore a valid .relay/profile.yaml.")
        else:
            run["reason"] = f"The repo {problem}, so Relay has no test command to run."
        return run
    command = report.profile["commands"]["test"]
    run["command"] = command
    run["test_report"] = report.profile.get("test_report")
    # A base with no readable profile counts as different: the command is new.
    run["command_differs_from_base"] = bool(base) and (
        (base_command, base_report) != (command, run["test_report"]))
    run["reason"] = None

    if run["commit"] is None:
        run["reason"] = "The working directory is not a git checkout, so a run would not belong to a commit."
        return run
    changed = uncommitted_changes(worktree)
    if changed:
        run["outcome"] = OUTCOME_UNCOMMITTED
        run["reason"] = (f"Not committed: {_listed(changed)}. "
                         "Commit the work (or ignore generated files) first.")
        return run

    report_path = _report_path(worktree, report.profile)
    started_at = time.time()
    started = time.monotonic()
    exit_code, timed_out = _run_command(command, worktree, log_path, timeout_seconds)
    run["duration_seconds"] = round(time.monotonic() - started, 1)
    run["exit_code"] = exit_code
    run["log"] = log_path.name
    if timed_out:
        run["outcome"] = OUTCOME_TIMED_OUT
        run["reason"] = f"The test command was stopped after {timeout_seconds} seconds."
    else:
        run["outcome"] = OUTCOME_PASSED if exit_code == 0 else OUTCOME_FAILED
    # Only a report this run wrote counts: an old file left on disk would
    # put a previous run's numbers on this one.
    if report_path is not None and _written_since(report_path, started_at):
        run["counts"] = parse_junit_report(report_path)

    # The run must still describe a commit afterwards. Tests that rewrite
    # tracked files, leave unignored output, or commit, break that.
    # A run that already failed keeps that outcome; the extra problem is
    # added to its reason.
    left_behind = uncommitted_changes(worktree)
    problem = None
    if head_commit(worktree) != run["commit"]:
        problem = "The test command moved the branch to a different commit while it ran."
    elif left_behind:
        problem = (f"The test command changed or created files that are not ignored: "
                   f"{_listed(left_behind)}. Add generated files to .gitignore.")
    if problem:
        if run["outcome"] == OUTCOME_PASSED:
            run["outcome"] = OUTCOME_UNCOMMITTED
        run["reason"] = " ".join(part for part in (run["reason"], problem) if part)
    return run


def _listed(paths: list[str]) -> str:
    return ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")


def _report_path(worktree: Path, profile: dict) -> Path | None:
    relative = profile.get("test_report")
    return worktree / relative if isinstance(relative, str) else None


def _written_since(path: Path, started_at: float) -> bool:
    try:
        # A second of slack: some filesystems round modification times down.
        return path.stat().st_mtime >= started_at - 1
    except OSError:
        return False


def summary(run: dict) -> str:
    """One line a person can read: what ran, where, and how it came out."""
    commit = (run.get("commit") or "no commit")[:12]
    outcome = run.get("outcome")
    if outcome in (OUTCOME_NOT_RUN, OUTCOME_UNCOMMITTED, OUTCOME_PROFILE_BROKEN):
        label = {OUTCOME_NOT_RUN: "not run", OUTCOME_UNCOMMITTED: "not accepted: uncommitted changes",
                 OUTCOME_PROFILE_BROKEN: "not run: the profile was removed or broken"}[outcome]
        return f"Tests {label} at {commit}. {run.get('reason') or ''}".strip()
    counts = run.get("counts")
    if counts:
        counted = (f"{counts['tests']} tests, {counts['failures'] + counts['errors']} failed, "
                   f"{counts['skipped']} skipped")
    else:
        counted = "counts unknown (the profile names no test report, or none was written)"
    label = {OUTCOME_PASSED: "passed", OUTCOME_FAILED: f"FAILED (exit code {run.get('exit_code')})",
             OUTCOME_TIMED_OUT: "TIMED OUT"}[outcome]
    line = f"Tests {label} at {commit} in {run.get('duration_seconds')}s: {counted}."
    if run.get("command_differs_from_base"):
        line += " The test command or report path is not the base branch's."
    return line
