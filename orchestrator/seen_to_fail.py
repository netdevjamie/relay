"""Seen to fail: did the branch's new or changed tests fail at its base?

A test that would pass without the change it is meant to prove proves
nothing. When a branch adds or changes test files, evidence.py runs those
files over the base's code in a scratch checkout. This module does the
reading: which test cases in the branch's report belong to the changed
files, and what each came to at the base. It runs nothing and keeps no
record; the result is stored with the run and shown, and never changes
where a task goes next.

Test cases are matched to files by the JUnit report's `file` attribute, or
by the leading words of its `classname` (pytest's `tests.test_x.TestY`
belongs to `.../tests/test_x.py`; JUnit's `com.a.BTest` to `.../com/a/BTest.java`).
A case that cannot be matched is not guessed at.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, replace

STATUS_CHECKED = "checked"
STATUS_COULD_NOT_CHECK = "could_not_check"

RESULT_FAILED = "failed"
RESULT_PASSED = "passed"
RESULT_DID_NOT_RUN = "did_not_run"
RESULT_COULD_NOT_CHECK = "could_not_check"

# What a JUnit test case came to.
CASE_PASSED, CASE_FAILURE, CASE_ERROR, CASE_SKIPPED = "passed", "failure", "error", "skipped"

MAX_PASSED_NAMES = 20
MAX_CASES = 200
MAX_MESSAGE = 100

NO_TEST_REPORT = "no test report"
NO_CASE_MATCHED = "no test case in the report matched the changed test files"
NO_TEST_RAN = "the run at the base stopped before any test ran"


@dataclass(frozen=True)
class Case:
    """One <testcase> of a JUnit report."""
    classname: str
    name: str
    file: str
    result: str
    message: str

    @property
    def test(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name

    @property
    def key(self) -> tuple[str, str]:
        return self.classname, self.name


def _without_extension(path: str) -> list[str]:
    stem, _extension = posixpath.splitext(path)
    return [part for part in stem.split("/") if part]


def belongs_to(case: Case, changed_file: str) -> bool:
    """True when the case is a test in `changed_file`."""
    if case.file:
        reported = posixpath.normpath(case.file)
        if (reported == changed_file or changed_file.endswith("/" + reported)
                or reported.endswith("/" + changed_file)):
            return True
    words = [word for word in case.classname.split(".") if word]
    file_words = _without_extension(changed_file)
    return any(words[:count] == file_words[-count:] for count in range(len(words), 0, -1)
               if count <= len(file_words))


def cases_in(cases: list[Case], changed_files: list[str]) -> list[Case]:
    """The cases that belong to a changed file and ran at the branch's
    commit (a case skipped there says nothing at the base)."""
    return [case for case in cases if case.result != CASE_SKIPPED
            and any(belongs_to(case, changed) for changed in changed_files)]


def could_not_check(reason: str, base_commit: str | None) -> dict:
    """The record when nothing could be checked at all."""
    return {"status": STATUS_COULD_NOT_CHECK, "reason": reason, "failed": 0, "passed": 0,
            "did_not_run": 0, "could_not_check": 0, "passed_names": [], "cases": [],
            "base_commit": base_commit}


def _load_error_for(case: Case, base_cases: list[Case], changed_files: list[str]) -> Case | None:
    """The base's error for the whole file this case is in, when the file
    could not be loaded there. pytest reports that as one case with no
    classname, named after the module (`tests.test_x`), holding <error>."""
    files = [changed for changed in changed_files if belongs_to(case, changed)]
    for found in base_cases:
        if found.result != CASE_ERROR:
            continue
        as_module = replace(found, classname=found.classname or found.name)
        if any(belongs_to(as_module, changed) for changed in files):
            return found
    return None


def _no_test_ran(base_cases: list[Case]) -> bool:
    """True when no test at the base passed, failed or was skipped: the run
    stopped before its tests (pytest runs none once a file fails to load)."""
    return not any(case.result in (CASE_PASSED, CASE_FAILURE, CASE_SKIPPED) for case in base_cases)


def _at_base(case: Case, base_cases: list[Case], changed_files: list[str],
             no_test_ran: bool) -> tuple[str, str | None]:
    """(result, why) for one of the branch's cases, from the base's run."""
    found = next((base_case for base_case in base_cases if base_case.key == case.key), None)
    if found is None:
        found = _load_error_for(case, base_cases, changed_files)
    if found is None and no_test_ran:
        return RESULT_COULD_NOT_CHECK, NO_TEST_RAN
    if found is None or found.result == CASE_SKIPPED:
        return RESULT_DID_NOT_RUN, None
    if found.result == CASE_FAILURE:
        return RESULT_FAILED, None
    if found.result == CASE_ERROR:
        # An error is a broken setup (an import that is not there yet, say),
        # not the test's own assertion failing.
        return RESULT_COULD_NOT_CHECK, f"errored at base: {found.message[:MAX_MESSAGE]}"
    return RESULT_PASSED, None


def classify(branch_cases: list[Case], base_cases: list[Case] | None, base_problem: str | None,
             base_commit: str | None, changed_files: list[str] = ()) -> dict:
    """The stored Seen to fail record for the branch's matched cases.
    `base_problem` says why the base run gave nothing to read (it timed
    out, wrote no readable report); then every case could not be checked.
    So it is when the base's report shows that no test ran."""
    no_test_ran = base_problem is None and _no_test_ran(base_cases or [])
    outcomes = []
    for case in branch_cases:
        if base_problem is not None:
            outcomes.append((case, RESULT_COULD_NOT_CHECK, base_problem))
        else:
            outcomes.append((case, *_at_base(case, base_cases or [], list(changed_files), no_test_ran)))
    tally = {result: sum(1 for _case, got, _why in outcomes if got == result)
             for result in (RESULT_FAILED, RESULT_PASSED, RESULT_DID_NOT_RUN, RESULT_COULD_NOT_CHECK)}
    reason = NO_TEST_RAN if no_test_ran else base_problem
    status = STATUS_COULD_NOT_CHECK if reason is not None else STATUS_CHECKED
    return {"status": status, "reason": reason, **tally,
            "passed_names": [case.test for case, got, _why in outcomes if got == RESULT_PASSED][:MAX_PASSED_NAMES],
            "cases": [{"test": case.test, "result": got, "why": why}
                      for case, got, why in outcomes][:MAX_CASES],
            "base_commit": base_commit}
