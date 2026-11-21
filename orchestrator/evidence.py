"""Test evidence: Relay runs a repo's tests itself and keeps the result.

An agent saying "the tests pass" proves nothing. After the Builder and the
Tester finish, `relay next` runs the test command from the repo's profile
(`.relay/profile.yaml`) at the task's commit, in its worktree or, in clean
mode, in a fresh checkout of the commit, and records the commit, the exit
code, how long it took and, when the profile names a JUnit report, the
counts. It may also run the base's command at the commit the branch left
its base at, in scratch checkouts, to count the tests there and to see the
branch's changed tests fail (Seen to fail). The roles and the routing act
on that record, not on the agent's description of it.

This module runs the command and reads the report. It never touches the
ledger: recorded_runs.py records what this returns.
"""
from __future__ import annotations

import dataclasses
import os
import re
import signal
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

import project_profile
import scratch_checkout
import seen_to_fail
import test_harness
from printable import printable

# What a run can come to (the `outcome` of a recorded run).
OUTCOME_PASSED = "passed"                # the command exited 0
# the command exited non-zero, or exited 0 with no tests run, without the
# report the profile names, or without the clean checkout it asks for
OUTCOME_FAILED = "failed"
OUTCOME_TIMED_OUT = "timed_out"          # the command ran past the time limit and was stopped
OUTCOME_UNCOMMITTED = "uncommitted"      # the worktree has changes that are not in a commit
OUTCOME_PROFILE_BROKEN = "profile_broken"  # the branch removed or broke the profile its base has
OUTCOME_NOT_RUN = "not_run"              # there was nothing Relay could run (no usable profile)

# Outcomes that mean the work is not ready, whatever the agent's verdict says.
FAILING_OUTCOMES = (OUTCOME_FAILED, OUTCOME_TIMED_OUT, OUTCOME_UNCOMMITTED, OUTCOME_PROFILE_BROKEN)

DEFAULT_TIMEOUT_SECONDS = 30 * 60
# On a timeout the run's process group is sent SIGTERM and given this long
# to clean up (servers, temporary databases, lock files) before SIGKILL.
TERM_GRACE_SECONDS = 10
# How often the grace period checks whether the group has gone.
_GROUP_POLL_SECONDS = 0.05
# The saved log keeps the end of the output, where test runners print
# failures and the summary.
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 50 * 1024 * 1024
# How much of the log a prompt quotes.
LOG_TAIL_LINES = 40
# What `relay next` prints, before the reason, when it asks the owner to
# confirm a test command and the base's profile was refused.
BASE_PROFILE_UNREAD = "The base branch's profile could not be read"
# Why the routing stops for the owner when only the test command or report
# path is not the base branch's (and has been since before harness files
# and test counts were checked).
TEST_COMMAND_AWAITS_OWNER = ("this task changed the test command or report path, so the owner "
                             "confirms it before the passing run is believed")
# Why Relay could not tell which harness files a branch changed
# (`harness_unchecked`); the owner then confirms the run.
# A branch with no history shared with its base has no such commit.
HARNESS_UNCHECKED_NO_FORK = ("the commit this branch left its base at could not be found, so "
                             "Relay could not tell which harness files it changed")
HARNESS_UNCHECKED_NO_DIFF = "git could not list the files this branch changed against its base"
HARNESS_UNCHECKED_TIP_UNREAD = ("the profile at the base commit {base} could not be read, so Relay could "
                                "not read the harness rules from it: {why}")
# How many characters of the base commit HARNESS_UNCHECKED_TIP_UNREAD shows.
BASE_COMMIT_SHOWN = 12
_FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Why a run's counts were not checked for 0 or fewer tests.
COUNTS_UNCHECKED_NO_REPORT = "the profile names no test report"
# What a drop in tests run is measured from (`tests_drop["reference"]`).
DROP_FROM_BASE = "base"
DROP_FROM_PREVIOUS = "previous run"
# How much of a failed run's reason its one-line summary quotes.
SUMMARY_REASON_LIMIT = 400


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


def is_full_commit_id(text) -> bool:
    """True for a full hex commit id (SHA-1 or SHA-256), never a ref name."""
    return isinstance(text, str) and _FULL_COMMIT_ID.fullmatch(text) is not None


def fork_point(worktree: Path, base_commit: str | None) -> str | None:
    """The commit the worktree's branch left the base at: the merge base of
    `base_commit` (the base commit Relay confirmed, base_trust.py) and HEAD.
    None when `base_commit` is not a full commit id, so no ref name is ever
    looked up here, or when git cannot say."""
    if not is_full_commit_id(base_commit):
        return None
    found = _git(worktree, "merge-base", base_commit, "HEAD")
    commit = found.stdout.strip()
    return commit if found.returncode == 0 and commit else None


def base_profile(worktree: Path, base_commit: str) -> tuple[object, str | None]:
    """(what the profile holds, why it was not read) for the profile at the
    point the task's branch left its base, as profile_at describes.

    The merge base is used, not the base commit, so a change to the test
    command that landed on the base after the task started is not held
    against it. The rules a branch must not escape are read from the base
    commit instead (base_rules)."""
    return profile_at(worktree, fork_point(worktree, base_commit) or base_commit)


def profile_at(worktree: Path, commit: str) -> tuple[object, str | None]:
    """(what the profile holds, why it was not read) for the profile at
    `commit`. The first is None when there is no profile there or Relay
    does not read it; the second is set, in one line, only when Relay
    refused it: larger than the size limit (its size is asked first), not
    UTF-8, or refused by bounded_yaml."""
    at_commit = f"{commit}:{project_profile.PROFILE_RELATIVE_PATH.as_posix()}"
    sized = _git(worktree, "cat-file", "-s", at_commit)
    if sized.returncode != 0:
        return None, None
    if not project_profile.fits_the_size_limit(sized.stdout):
        return None, f"it {project_profile.TOO_LARGE}."
    try:
        shown = _git(worktree, "show", at_commit)
    except UnicodeDecodeError:
        return None, "it is not UTF-8."
    if shown.returncode != 0:
        return None, None
    try:
        return project_profile.profile_in(shown.stdout), None
    except project_profile.Unreadable as problem:
        # YAML's own errors take several lines; the reason is printed in one.
        return None, " ".join(problem.why.split())


def base_rules(worktree: Path, base_commit: str, at_fork) -> tuple[object, str | None]:
    """(the profile `test_harness` and `test_clean_checkout` are read from,
    why the harness could not be checked against it). They come from the
    base commit Relay confirmed, which only moves forward, so a branch
    cannot escape a rule the owner added to the base by building on an
    older commit of it. When that profile was refused, the profile at the
    fork (`at_fork`) is used for clean mode and the reason is set, so the
    run waits for the owner. A base commit with no profile has no rules."""
    data, unread = profile_at(worktree, base_commit)
    if unread:
        return at_fork, HARNESS_UNCHECKED_TIP_UNREAD.format(base=base_commit[:BASE_COMMIT_SHOWN],
                                                            why=unread)
    return data, None


def _test_setup_in(data) -> tuple[str | None, str | None]:
    """(test command, test report path) from what a profile holds."""
    if not isinstance(data, dict):
        return None, None
    commands = data.get("commands")
    command = commands.get("test") if isinstance(commands, dict) else None
    report = data.get("test_report")
    return (command if isinstance(command, str) else None,
            report if isinstance(report, str) else None)


@dataclass
class TestRun:
    """What one run of a repo's tests came to, as it is stored in the ledger.

    The first twelve fields are the keys every recorded run has had, in the
    order they are stored. The fields after them came later: each defaults
    to "not flagged" or "not checked", which is also how a reader must take
    a record that lacks them. Records are read back as plain dicts and never
    rebuilt into this class, so an old record reads as it was written."""
    # pytest collects classes whose names start with "Test"; this is not one.
    __test__ = False

    outcome: str
    reason: str | None = None
    commit: str | None = None
    command: str | None = None
    test_report: str | None = None
    command_differs_from_base: bool = False
    base_command: str | None = None
    base_profile_unread: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    counts: dict | None = None
    log: str | None = None
    # The report's tests minus its skipped, when the report was read.
    tests_run: int | None = None
    # Why the counts were not checked for 0 or fewer tests, when they were not.
    counts_unchecked: str | None = None
    # Harness files the branch changed against the base, and a digest of
    # their content at this commit (what an owner's acceptance covers).
    harness_changed: list[str] = field(default_factory=list)
    harness_digest: str | None = None
    # Tests run by the latest earlier run of this task that had counts.
    previous_tests_run: int | None = None
    # The commit the branch left its base at, and the tests run there.
    base_commit: str | None = None
    base_tests_run: int | None = None
    base_count_unknown: str | None = None
    # {"fewer", "reference", "before", "now"}: the larger drop in tests run.
    tests_drop: dict | None = None
    clean_checkout: bool = False
    # Top-level ignored paths present in the worktree when the run started.
    ignored_present: list[str] | None = None
    seen_to_fail: dict | None = None
    base_log: str | None = None
    seen_to_fail_log: str | None = None
    # Why the harness files could not be checked, when they could not.
    harness_unchecked: str | None = None

    def to_record(self) -> dict:
        """The record stored in the ledger: every field, in field order."""
        return dataclasses.asdict(self)


def not_run(reason: str, commit: str | None = None) -> TestRun:
    """The record of a run Relay could not make."""
    return TestRun(outcome=OUTCOME_NOT_RUN, reason=reason, commit=commit)


def _junit_suites(path: Path) -> list | None:
    """The <testsuite> elements of a JUnit XML report, or None when the file
    is missing, too big, not XML, or not a JUnit report. A single
    <testsuite> or a <testsuites> wrapper is accepted."""
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
    return suites or None


def parse_junit_report(path: Path) -> dict | None:
    """Counts from a JUnit XML report: {"tests", "failures", "errors", "skipped"}.

    None when the file is missing, too big, not XML, or not a JUnit report.
    Accepts a single <testsuite> or a <testsuites> wrapper; nested suites
    are not counted twice."""
    suites = _junit_suites(path)
    if suites is None:
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


def junit_cases(path: Path) -> list[seen_to_fail.Case] | None:
    """Every <testcase> in a JUnit XML report, with what it came to, or None
    when the report is refused for any reason parse_junit_report refuses it."""
    suites = _junit_suites(path)
    if suites is None:
        return None
    return [_case_of(element) for suite in suites for element in suite.iter("testcase")]


def _case_of(element) -> seen_to_fail.Case:
    """A failure or an error outranks a skip, whatever order they come in."""
    outcomes = {child.tag: child for child in element
                if child.tag in (seen_to_fail.CASE_FAILURE, seen_to_fail.CASE_ERROR, seen_to_fail.CASE_SKIPPED)}
    result = next((tag for tag in (seen_to_fail.CASE_FAILURE, seen_to_fail.CASE_ERROR, seen_to_fail.CASE_SKIPPED)
                   if tag in outcomes), seen_to_fail.CASE_PASSED)
    message = ""
    if result in outcomes:
        said = (outcomes[result].get("message") or outcomes[result].text or "").strip()
        message = said.splitlines()[0] if said else ""
    return seen_to_fail.Case(classname=element.get("classname") or "", name=element.get("name") or "",
                             file=element.get("file") or "", result=result, message=message)


def _run_command(command: str, worktree: Path, log_path: Path, timeout_seconds: int, *,
                 stop_leftovers: bool = False) -> tuple[int | None, bool]:
    """Run one line of shell from the worktree, saving its output.
    Returns (exit code, timed out). The exit code is None on a timeout.

    With `stop_leftovers`, anything the command started and left running
    after it exited is stopped too, so nothing is still writing into a
    scratch folder when it is deleted."""
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
            if stop_leftovers and _group_is_running(process.pid):
                _stop_group(process)
        except subprocess.TimeoutExpired:
            _stop_group(process)
            exit_code, timed_out = None, True
    _keep_end_of_log(log_path)
    return exit_code, timed_out


def _group_is_running(group: int) -> bool:
    """True while any process is left in the group. Signal 0 asks without
    sending anything."""
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A member Relay may not signal is still a member.
        return True
    return True


def _stop_group(process: subprocess.Popen) -> None:
    """Ask the process group to stop (SIGTERM), wait up to the grace period
    for it to go, then SIGKILL whatever is left. A group that goes within
    the grace gets no SIGKILL. The group's id is the shell's pid."""
    group = process.pid
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        # Reap the shell once it exits: an unreaped shell still counts as
        # a member of the group.
        process.poll()
        if not _group_is_running(group):
            process.wait()
            return
        time.sleep(_GROUP_POLL_SECONDS)
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


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


def run_tests(worktree: Path, log_path: Path, *, base_commit: str | None = None,
              timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
              earlier_runs: list[dict] | tuple = ()) -> TestRun:
    """Run the profile's test command in `worktree` and describe what happened.

    The profile is read from the worktree, because a task may rightly change
    how its repo is tested. But the branch under test then decides what
    "the tests" are, so what the owner must confirm before a pass is
    believed is flagged: a command or report path that is not the base
    branch's (`command_differs_from_base`), harness files the branch changed
    (`harness_changed`), and fewer tests run than at the base or in the
    previous run (`tests_drop`). Every rule for what is checked comes from
    the base, never from the branch: the base's command and the files it
    names from the commit the branch left the base at, the `test_harness`
    list and clean mode from the base commit Relay confirmed
    (`base_commit`, a full commit id; None when the task has no base).

    A passing exit code is not enough: when the profile names a report, 0
    tests run, or no readable report, is a failure. When the branch changed
    test files, they are run over the base's code (Seen to fail); that is
    recorded and shown, and changes nothing else.

    `earlier_runs` are this task's recorded runs, oldest first, as stored.
    Returns a TestRun; its to_record() is what is stored.
    `base_profile_unread` is why the base's profile was refused, when it was.
    """
    if base_commit is not None and not is_full_commit_id(base_commit):
        raise ValueError(f"base_commit must be a full commit id, not {base_commit!r}")
    run = not_run("", commit=head_commit(worktree))
    # No base at all (a task with no branch of its own) leaves nothing to
    # compare with.
    base_data, run.base_profile_unread = (base_profile(worktree, base_commit) if base_commit
                                          else (None, None))
    base_command, base_report = _test_setup_in(base_data)
    run.base_command = base_command

    report = project_profile.load_profile(worktree)
    if not report.valid:
        problem = "has no project profile" if not report.found else "has a project profile with errors"
        if base_command is not None:
            # The base could be tested and this branch can't: that is the
            # branch's doing, and it must not be a way to skip the tests.
            run.outcome = OUTCOME_PROFILE_BROKEN
            run.command_differs_from_base = True
            run.reason = (f"This branch {problem}, but its base has a test command "
                          f"({base_command}). Restore a valid .relay/profile.yaml.")
        else:
            run.reason = f"The repo {problem}, so Relay has no test command to run."
        return run
    command = report.profile["commands"]["test"]
    run.command = command
    run.test_report = report.profile.get("test_report")
    # A base with no readable profile counts as different: the command is new.
    run.command_differs_from_base = bool(base_commit) and (
        (base_command, base_report) != (command, run.test_report))
    run.reason = None

    if run.commit is None:
        run.reason = "The working directory is not a git checkout, so a run would not belong to a commit."
        return run
    changed = uncommitted_changes(worktree)
    if changed:
        run.outcome = OUTCOME_UNCOMMITTED
        run.reason = (f"Not committed: {_listed(changed)}. "
                      "Commit the work (or ignore generated files) first.")
        return run

    rules, rules_unread = base_rules(worktree, base_commit, base_data) if base_commit else (None, None)
    base_setup = _BaseSetup(fork=fork_point(worktree, base_commit), rules=rules,
                            rules_unread=rules_unread, command=base_command, report=base_report,
                            base_commit=base_commit)
    changed_tests = _look_at_the_branch(run, worktree, base_setup)
    run.clean_checkout = _asks_for_clean_checkout(rules) or _asks_for_clean_checkout(report.profile)
    if not run.clean_checkout:
        run.ignored_present = test_harness.ignored_top_level(worktree)

    ran = _run_at_the_commit(run, worktree, log_path, timeout_seconds, want_cases=bool(changed_tests))
    if ran is None:
        return run
    run.duration_seconds, run.exit_code, run.log = ran.duration_seconds, ran.exit_code, log_path.name
    if ran.timed_out:
        run.outcome = OUTCOME_TIMED_OUT
        run.reason = f"The test command was stopped after {timeout_seconds} seconds."
    else:
        run.outcome = OUTCOME_PASSED if ran.exit_code == 0 else OUTCOME_FAILED
    run.counts = ran.counts
    _check_the_counts(run)
    _check_nothing_was_left_behind(run, worktree)

    if base_commit and run.outcome == OUTCOME_PASSED and run.tests_run:
        _compare_the_counts(run, worktree, base_setup, earlier_runs, log_path, timeout_seconds)
    if run.outcome == OUTCOME_PASSED and changed_tests:
        run.seen_to_fail, run.seen_to_fail_log = _see_them_fail_at_base(
            run, worktree, base_setup, changed_tests, ran.cases, log_path, timeout_seconds)
    return run


@dataclass(frozen=True)
class _BaseSetup:
    """What the run reads from the base: the commit the branch left it at,
    and that commit's test command and report path; the profile the
    harness list and clean mode come from (`rules`, the base commit: see
    base_rules) and why the harness cannot be checked against it
    (`rules_unread`). `base_commit` is the base commit Relay confirmed,
    None when the task has no base."""
    fork: str | None
    rules: object
    rules_unread: str | None
    command: str | None
    report: str | None
    base_commit: str | None = None


@dataclass(frozen=True)
class _Ran:
    """One run of a command in a folder, and the report it wrote."""
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    counts: dict | None
    cases: list | None


def _look_at_the_branch(run: TestRun, worktree: Path, base_setup: _BaseSetup) -> list[str]:
    """Record the harness files the branch changed against its fork point,
    and return the test files it added or changed. When a base is set but
    the harness cannot be checked against it, record why
    (`harness_unchecked`), so the run waits for the owner instead of
    reading as a plain pass."""
    if base_setup.fork is None:
        if base_setup.base_commit:
            run.harness_unchecked = HARNESS_UNCHECKED_NO_FORK
        return []
    if base_setup.rules_unread:
        run.harness_unchecked = base_setup.rules_unread
        return []
    is_harness = test_harness.harness_rule(worktree, base_setup.fork, base_setup.command, base_setup.rules)
    try:
        run.harness_changed, run.harness_digest = test_harness.changed_harness(
            worktree, base_setup.fork, run.commit, is_harness)
        return test_harness.changed_test_files(worktree, base_setup.fork, run.commit, is_harness)
    except test_harness.HarnessUnreadable:
        run.harness_unchecked = HARNESS_UNCHECKED_NO_DIFF
        return []


def _asks_for_clean_checkout(profile_data) -> bool:
    return isinstance(profile_data, dict) and profile_data.get("test_clean_checkout") is True


def _run_at_the_commit(run: TestRun, worktree: Path, log_path: Path, timeout_seconds: int,
                       want_cases: bool) -> _Ran | None:
    """Run the branch's command: in the worktree, or in a fresh checkout of
    the commit when clean mode is on. None (with the run marked failed) when
    the fresh checkout could not be made."""
    if not run.clean_checkout:
        return _run_and_read(run.command, worktree, run.test_report, log_path, timeout_seconds,
                             in_scratch=False, want_cases=want_cases)
    try:
        with scratch_checkout.scratch_checkout(worktree, run.commit) as folder:
            return _run_and_read(run.command, folder, run.test_report, log_path, timeout_seconds,
                                 in_scratch=True, want_cases=want_cases)
    except scratch_checkout.ScratchCheckoutFailed as problem:
        # Never a pass Relay did not see: the profile asked for a clean run.
        run.outcome = OUTCOME_FAILED
        run.reason = f"Relay could not make the clean checkout the profile asks for. {problem}"
        return None


def _run_and_read(command: str, folder: Path, report_relative: str | None, log_path: Path,
                  timeout_seconds: int, *, in_scratch: bool, want_cases: bool) -> _Ran:
    """Run a test command in `folder` and read the report it wrote there."""
    report_path = folder / report_relative if report_relative and _is_inside(report_relative) else None
    started_at = time.time()
    started = time.monotonic()
    exit_code, timed_out = _run_command(command, folder, log_path, timeout_seconds,
                                        stop_leftovers=in_scratch)
    duration = round(time.monotonic() - started, 1)
    counts = cases = None
    # Only a report this run wrote counts: an old file left on disk would
    # put a previous run's numbers on this one.
    if report_path is not None and _written_since(report_path, started_at):
        counts = parse_junit_report(report_path)
        if counts is not None and want_cases:
            cases = junit_cases(report_path)
    return _Ran(exit_code, timed_out, duration, counts, cases)


def _is_inside(relative: str) -> bool:
    path = Path(relative)
    return not path.is_absolute() and ".." not in path.parts


def tests_run_in(counts: dict | None) -> int | None:
    """Tests run, from a report's counts: its tests minus its skipped, so a
    test turned into a skip counts as one fewer."""
    if not isinstance(counts, dict):
        return None
    try:
        return max(0, int(counts["tests"]) - int(counts["skipped"]))
    except (KeyError, TypeError, ValueError):
        return None


def _check_the_counts(run: TestRun) -> None:
    """A passing exit code with no tests run, or with no readable report
    where the profile names one, is a failure."""
    run.tests_run = tests_run_in(run.counts)
    if run.test_report is None:
        run.counts_unchecked = COUNTS_UNCHECKED_NO_REPORT
        return
    if run.outcome != OUTCOME_PASSED:
        return
    if run.counts is None:
        run.outcome = OUTCOME_FAILED
        run.reason = (f"The profile names {printable(run.test_report)}, but this run did not "
                      "write a readable report.")
    elif run.tests_run == 0:
        run.outcome = OUTCOME_FAILED
        run.reason = (f"0 tests ran (the report counts {run.counts['tests']} tests, "
                      f"{run.counts['skipped']} of them skipped).")


def _check_nothing_was_left_behind(run: TestRun, worktree: Path) -> None:
    """The run must still describe a commit afterwards. Tests that rewrite
    tracked files, leave unignored output, or commit, break that. A run
    that already failed keeps that outcome; the extra problem is added to
    its reason."""
    left_behind = uncommitted_changes(worktree)
    problem = None
    if head_commit(worktree) != run.commit:
        problem = "The test command moved the branch to a different commit while it ran."
    elif left_behind:
        problem = (f"The test command changed or created files that are not ignored: "
                   f"{_listed(left_behind)}. Add generated files to .gitignore.")
    if problem:
        if run.outcome == OUTCOME_PASSED:
            run.outcome = OUTCOME_UNCOMMITTED
        run.reason = " ".join(part for part in (run.reason, problem) if part)


def _compare_the_counts(run: TestRun, worktree: Path, base_setup: _BaseSetup, earlier_runs,
                        log_path: Path, timeout_seconds: int) -> None:
    """Record the tests run at the base and in the previous run, and the
    larger drop from either to this run."""
    run.previous_tests_run = _previous_tests_run(earlier_runs)
    run.base_commit = base_setup.fork
    if base_setup.fork is None:
        run.base_count_unknown = "the commit this branch left its base at could not be found"
    elif base_setup.fork == run.commit:
        run.base_tests_run = run.tests_run
    else:
        recorded = _recorded_base_count(earlier_runs, base_setup.fork)
        if recorded is not None:
            run.base_tests_run, run.base_count_unknown = recorded
        else:
            run.base_tests_run, run.base_count_unknown, run.base_log = _count_at_base(
                worktree, base_setup, log_path, timeout_seconds)
    run.tests_drop = _larger_drop(run)


def _previous_tests_run(earlier_runs) -> int | None:
    """Tests run by the latest earlier run that ran and had counts. A record
    from before `tests_run` was stored is worked out from its counts."""
    for earlier in reversed(list(earlier_runs)):
        if earlier.get("outcome") not in (OUTCOME_PASSED, OUTCOME_FAILED):
            continue
        counted = earlier.get("tests_run")
        if not isinstance(counted, int) or isinstance(counted, bool):
            counted = tests_run_in(earlier.get("counts"))
        if counted is not None:
            return counted
    return None


def _recorded_base_count(earlier_runs, fork: str) -> tuple[int | None, str | None] | None:
    """(tests run at base, why unknown) from an earlier run at the same fork
    point, so the base is measured at most once per fork point per task."""
    for earlier in reversed(list(earlier_runs)):
        if earlier.get("base_commit") != fork:
            continue
        counted, unknown = earlier.get("base_tests_run"), earlier.get("base_count_unknown")
        if isinstance(counted, int) and not isinstance(counted, bool):
            return counted, None
        if isinstance(unknown, str):
            return None, unknown
    return None


def _count_at_base(worktree: Path, base_setup: _BaseSetup, log_path: Path,
                   timeout_seconds: int) -> tuple[int | None, str | None, str | None]:
    """(tests run, why unknown, log name) for the base's own command run at
    the fork point in a scratch checkout."""
    if base_setup.command is None:
        return None, "the base has no test command", None
    if base_setup.report is None:
        return None, "the base's profile names no test report", None
    base_log = _log_beside(log_path, "base")
    try:
        with scratch_checkout.scratch_checkout(worktree, base_setup.fork) as folder:
            ran = _run_and_read(base_setup.command, folder, base_setup.report, base_log,
                                timeout_seconds, in_scratch=True, want_cases=False)
    except scratch_checkout.ScratchCheckoutFailed as problem:
        return None, str(problem), None
    if ran.timed_out:
        return None, f"the run at the base was stopped after {timeout_seconds} seconds", base_log.name
    counted = tests_run_in(ran.counts)
    if counted is None:
        return None, "the run at the base wrote no readable report", base_log.name
    return counted, None, base_log.name


def _log_beside(log_path: Path, kind: str) -> Path:
    """`03-test-run.log` -> `03-test-run-base.log`, for an extra run's log."""
    return log_path.with_name(f"{log_path.stem}-{kind}{log_path.suffix}")


def _larger_drop(run: TestRun) -> dict | None:
    """The larger drop in tests run, from the base or the previous run, or
    None when neither ran more tests than this run. The base wins a tie."""
    drops = [(before, reference) for before, reference in
             ((run.base_tests_run, DROP_FROM_BASE), (run.previous_tests_run, DROP_FROM_PREVIOUS))
             if before is not None and before > run.tests_run]
    if not drops:
        return None
    before, reference = max(drops, key=lambda drop: drop[0])
    return {"fewer": before - run.tests_run, "reference": reference, "before": before,
            "now": run.tests_run}


def _see_them_fail_at_base(run: TestRun, worktree: Path, base_setup: _BaseSetup,
                           changed_tests: list[str], branch_cases: list | None, log_path: Path,
                           timeout_seconds: int) -> tuple[dict, str | None]:
    """(Seen to fail record, log name): the branch's changed test files run
    over the base's code in a scratch checkout, each of their cases read."""
    fork = base_setup.fork
    if branch_cases is None or base_setup.report is None:
        return seen_to_fail.could_not_check(seen_to_fail.NO_TEST_REPORT, fork), None
    if base_setup.command is None:
        return seen_to_fail.could_not_check("the base has no test command", fork), None
    matched = seen_to_fail.cases_in(branch_cases, changed_tests)
    if not matched:
        return seen_to_fail.could_not_check(seen_to_fail.NO_CASE_MATCHED, fork), None
    seen_log = _log_beside(log_path, "seen-to-fail")
    try:
        with scratch_checkout.scratch_checkout(worktree, fork, overlay_from=run.commit,
                                               overlay_paths=changed_tests) as folder:
            ran = _run_and_read(base_setup.command, folder, base_setup.report, seen_log,
                                timeout_seconds, in_scratch=True, want_cases=True)
    except scratch_checkout.ScratchCheckoutFailed as problem:
        return seen_to_fail.could_not_check(str(problem), fork), None
    problem = None
    if ran.timed_out:
        problem = f"the run at the base was stopped after {timeout_seconds} seconds"
    elif ran.cases is None:
        problem = "the run at the base wrote no readable report"
    return seen_to_fail.classify(matched, ran.cases, problem, fork, changed_tests), seen_log.name


def _listed(paths: list[str]) -> str:
    return ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")


def listed_printable(paths: list[str]) -> str:
    """Up to five branch-written names, each made printable, then "and N more"."""
    return _listed([printable(path) for path in paths])


def _written_since(path: Path, started_at: float) -> bool:
    try:
        # A second of slack: some filesystems round modification times down.
        return path.stat().st_mtime >= started_at - 1
    except OSError:
        return False


def drop_sentence(drop: dict) -> str:
    """"N fewer tests ran than at the base (B before, H now)", or "... than
    the previous run"."""
    where = "at the base" if drop.get("reference") == DROP_FROM_BASE else "the previous run"
    return (f"{drop.get('fewer')} fewer tests ran than {where} "
            f"({drop.get('before')} before, {drop.get('now')} now)")


def needs_owner_confirmation(run: dict) -> bool:
    """True when a passing run is believed only once the owner confirms it:
    the command or report path is not the base's, harness files changed or
    could not be checked, or fewer tests ran. A record without the later
    keys reads as not flagged."""
    return bool(run.get("command_differs_from_base") or run.get("harness_changed")
                or run.get("harness_unchecked") or run.get("tests_drop"))


def confirmation_reason(run: dict) -> str | None:
    """Why the owner must confirm this run, in the words routing gives, or
    None. A changed command alone keeps the words it has always had."""
    if not needs_owner_confirmation(run):
        return None
    harness, drop = run.get("harness_changed"), run.get("tests_drop")
    unchecked = run.get("harness_unchecked")
    if not harness and not drop and not unchecked:
        return TEST_COMMAND_AWAITS_OWNER
    changes = []
    if run.get("command_differs_from_base"):
        changes.append("changed the test command or report path")
    if harness:
        changes.append(f"changed test harness files ({listed_printable(harness)})")
    causes = ["this task " + " and ".join(changes)] if changes else []
    if unchecked:
        causes.append(f"Relay could not check the harness files ({printable(unchecked)})")
    if isinstance(drop, dict):
        causes.append(drop_sentence(drop))
    return "; ".join(causes) + ", so the owner confirms it before the passing run is believed"


def seen_to_fail_line(run: dict) -> str | None:
    """"Seen to fail at base: X failed, Y passed, Z could not check", then
    the tests that passed at base; None when nothing was checked."""
    result = run.get("seen_to_fail")
    if not isinstance(result, dict):
        return None
    if result.get("status") == seen_to_fail.STATUS_COULD_NOT_CHECK and not result.get("cases"):
        return f"Seen to fail at base: could not check ({printable(result.get('reason'))})"
    line = (f"Seen to fail at base: {result.get('failed', 0)} failed, {result.get('passed', 0)} passed, "
            f"{result.get('could_not_check', 0)} could not check")
    if result.get("did_not_run"):
        line += f", {result['did_not_run']} did not run"
    if result.get("reason"):
        line += f" ({printable(result['reason'])})"
    names = result.get("passed_names") or []
    if names:
        line += f". Passed at base: {listed_printable(names)}"
    return line


def summary(run: dict) -> str:
    """One line a person can read: what ran, where, and how it came out.
    A record without the later keys still reads, with nothing said about
    what they would hold."""
    commit = (run.get("commit") or "no commit")[:12]
    outcome = run.get("outcome")
    if outcome in (OUTCOME_NOT_RUN, OUTCOME_UNCOMMITTED, OUTCOME_PROFILE_BROKEN):
        label = {OUTCOME_NOT_RUN: "not run", OUTCOME_UNCOMMITTED: "not accepted: uncommitted changes",
                 OUTCOME_PROFILE_BROKEN: "not run: the profile was removed or broken"}[outcome]
        return f"Tests {label} at {commit}. {run.get('reason') or ''}".strip()
    counts = run.get("counts")
    if run.get("counts_unchecked"):
        counted = f"counts unknown ({run['counts_unchecked']}), so Relay could not check for 0 or fewer tests"
    elif counts:
        counted = (f"{counts['tests']} tests, {counts['failures'] + counts['errors']} failed, "
                   f"{counts['skipped']} skipped")
    else:
        counted = "counts unknown (the profile names no test report, or none was written)"
    if outcome == OUTCOME_FAILED and run.get("exit_code") == 0:
        # Exit 0 that is still a failure: 0 tests, or no readable report.
        failed = f"FAILED ({printable(run.get('reason'), SUMMARY_REASON_LIMIT)})"
    else:
        failed = f"FAILED (exit code {run.get('exit_code')})"
    label = {OUTCOME_PASSED: "passed", OUTCOME_FAILED: failed, OUTCOME_TIMED_OUT: "TIMED OUT"}[outcome]
    line = f"Tests {label} at {commit} in {run.get('duration_seconds')}s: {counted}."
    if run.get("command_differs_from_base"):
        line += " The test command or report path is not the base branch's."
    return line + "".join(f" {sentence}." for sentence in _later_sentences(run))


def _later_sentences(run: dict) -> list[str]:
    """What the later keys add to the summary, each only when its key says
    something."""
    sentences = []
    if run.get("harness_changed"):
        sentences.append(f"Harness files changed against the base: {listed_printable(run['harness_changed'])}")
    if run.get("harness_unchecked"):
        sentences.append(f"Harness files not checked: {printable(run['harness_unchecked'])}")
    if isinstance(run.get("tests_drop"), dict):
        sentences.append(drop_sentence(run["tests_drop"]))
    if run.get("base_count_unknown"):
        sentences.append(f"The base's test count is unknown ({printable(run['base_count_unknown'])})")
    if run.get("clean_checkout"):
        sentences.append("Ran in a clean checkout")
    elif run.get("ignored_present"):
        sentences.append(f"Ran with ignored files present: {listed_printable(run['ignored_present'])}")
    seen = seen_to_fail_line(run)
    if seen:
        sentences.append(seen)
    return sentences
