"""Seen to fail: a branch's new or changed tests are run over the base's code.

Each of their test cases is recorded as failed, passed, did not run, or
could not check at the base. The result is shown with the run and on the
gate, and never changes where the task goes next.
"""

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import evidence
import read_api
import scratch_checkout
import seen_to_fail
import settings
from seen_to_fail import Case
from tests.git_fixtures import cases_repo, commit_all, commit_of, git, write_cases, write_profile  # noqa: F401  (fixtures)
from tests.test_evidence import UP_TO_BUILD, current, task_in_repo
from tests.arbiter_fixtures import stand_at_the_gate
from tests.test_send_back import config, finish_step  # noqa: F401  (config is a fixture)

# The reason Seen to fail gives when the base run stopped before any test ran.
STOPPED_BEFORE_TESTS = "the run at the base stopped before any test ran"


def feature_branch(repo: Path, new_case: str = "needs src/feature.txt") -> None:
    """A branch that adds src/feature.txt and a test file with a new test
    that needs it and an old one that passes anyway."""
    git(repo, "checkout", "-q", "-b", "relay/task")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "feature.txt").write_text("the feature\n")
    write_cases(repo, "test_b", {"test_new": new_case, "test_old": "pass"})
    commit_all(repo)


def worktree_state(repo: Path) -> tuple[str, str]:
    return (git(repo, "status", "--porcelain"), git(repo, "status", "--porcelain", "--ignored"))


def scratch_folders() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob(f"{scratch_checkout.FOLDER_PREFIX}*"))


class TestSeenToFailRuns:
    def test_new_tests_are_run_at_base_and_each_case_is_recorded(self, config, cases_repo, capsys):
        task_in_repo("seen", config, cases_repo, UP_TO_BUILD)
        feature_branch(cases_repo)
        finish_step("seen", config, "ready-for-review")
        assert commands.cmd_next("seen", auto=False, config=config) == 0
        finish_step("seen", config, "approved")
        assert commands.cmd_next("seen", auto=False, config=config) == 0
        # A Tester's concerns go to the Arbiter; `relay next`
        # shows the run's result as it moves on.
        capsys.readouterr()
        finish_step("seen", config, "pass_with_concerns")
        assert commands.cmd_next("seen", auto=False, config=config) == 0
        out = capsys.readouterr().out
        assert "Next phase: arbiter" in out
        run = read_api.status_payload("seen", config)["test_runs"][-1]
        result = run["seen_to_fail"]
        assert (result["status"], result["failed"], result["passed"], result["could_not_check"]) == (
            "checked", 1, 1, 0)
        assert result["cases"] == [{"test": "tests.test_b::test_new", "result": "failed", "why": None},
                                   {"test": "tests.test_b::test_old", "result": "passed", "why": None}]
        assert result["base_commit"] == evidence.fork_point(cases_repo, commit_of(cases_repo))
        line = ("Seen to fail at base: 1 failed, 1 passed, 0 could not check. "
                "Passed at base: tests.test_b::test_old")
        assert line + "." in evidence.summary(run)
        assert f"  {evidence.summary(run)}\n" in out
        assert run["seen_to_fail_log"] == "05-test-run-seen-to-fail.log"
        # It ran at the Builder's step too, on the same commit.
        assert (settings.TASKS_DIR / "seen" / "03-test-run-seen-to-fail.log").exists()

    def test_the_result_is_shown_on_an_approval_gate(self, config, cases_repo, capsys):
        """The gate prints the seen-to-fail line on its own line, apart from
        the run's summary. A warning verdict now goes to the Arbiter, so the
        gate is reached the way a `relay next` from before the Arbiter left
        it (stand_at_the_gate): the walk above no longer stops at one."""
        task_in_repo("seen-gate", config, cases_repo, UP_TO_BUILD)
        feature_branch(cases_repo)
        finish_step("seen-gate", config, "ready-for-review")
        assert commands.cmd_next("seen-gate", auto=False, config=config) == 0
        finish_step("seen-gate", config, "approved")
        assert commands.cmd_next("seen-gate", auto=False, config=config) == 0
        finish_step("seen-gate", config, "pass_with_concerns")
        stand_at_the_gate("seen-gate", config)
        capsys.readouterr()
        assert commands.cmd_next("seen-gate", auto=False, config=config) == 0
        out = capsys.readouterr().out
        assert "HUMAN GATE: Approval required." in out
        line = ("Seen to fail at base: 1 failed, 1 passed, 0 could not check. "
                "Passed at base: tests.test_b::test_old")
        assert f"  {line}\n" in out

    def test_seen_to_fail_never_changes_routing(self, config, cases_repo):
        task_in_repo("seen-route", config, cases_repo, UP_TO_BUILD)
        feature_branch(cases_repo, new_case="pass")
        finish_step("seen-route", config, "ready-for-review")
        assert commands.cmd_next("seen-route", auto=False, config=config) == 0
        run = read_api.status_payload("seen-route", config)["test_runs"][-1]
        assert run["seen_to_fail"]["passed"] == 2
        assert current("seen-route", config) == ("in_progress", "reviewer")

    def test_an_import_error_at_base_is_could_not_check_not_failed(self, cases_repo, tmp_path):
        """The case errors at base (the runner's "error" is what a missing
        import gives) only on the base's code: it passes on the branch's."""
        (cases_repo / "src").mkdir()
        (cases_repo / "src" / "mode.txt").write_text("error\n")
        (cases_repo / "runner.py").write_text((cases_repo / "runner.py").read_text().replace(
            'how = "pass" if os.path.exists(how[6:]) else "fail"',
            'how = "pass" if os.path.exists(how[6:]) else open("src/mode.txt").read().strip()'))
        commit_all(cases_repo, "a base where a missing feature is an error")
        feature_branch(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        result = run.seen_to_fail
        assert (result["failed"], result["passed"], result["could_not_check"]) == (0, 1, 1)
        assert result["cases"][0] == {"test": "tests.test_b::test_new", "result": "could_not_check",
                                      "why": "errored at base: ImportError: no feature"}

    def test_no_report_gives_could_not_check_and_runs_nothing(self, cases_repo, tmp_path, monkeypatch):
        folders = tmp_path / "folders.txt"
        monkeypatch.setenv("RUNNER_CWD_LOG", str(folders))
        write_profile(cases_repo, "python3 runner.py")
        commit_all(cases_repo, "no report named")
        feature_branch(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        assert run.seen_to_fail == seen_to_fail.could_not_check("no test report", run.base_commit
                                                                or evidence.fork_point(cases_repo, commit_of(cases_repo)))
        assert run.seen_to_fail_log is None
        assert len(folders.read_text().splitlines()) == 1
        assert "Seen to fail at base: could not check (no test report)." in evidence.summary(run.to_record())

    def test_the_worktree_is_untouched_and_the_scratch_is_removed_even_on_timeout(
            self, cases_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(evidence, "TERM_GRACE_SECONDS", 1)
        (cases_repo / "runner.py").write_text(
            "import os, time\nif not os.path.exists('src/feature.txt'):\n    time.sleep(30)\n"
            + (cases_repo / "runner.py").read_text())
        commit_all(cases_repo, "a base that hangs without the feature")
        feature_branch(cases_repo)
        # The run in the worktree writes its own (ignored) report there.
        (cases_repo / "reports").mkdir()
        (cases_repo / "reports" / "junit.xml").write_text("")
        before, folders_before = worktree_state(cases_repo), scratch_folders()
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo), timeout_seconds=3)
        assert run.outcome == "passed"
        assert run.seen_to_fail["status"] == "could_not_check"
        assert run.seen_to_fail["could_not_check"] == 2
        assert run.seen_to_fail["reason"] == "the run at the base was stopped after 3 seconds"
        assert worktree_state(cases_repo) == before
        assert scratch_folders() == folders_before

    def test_a_branch_that_changes_no_test_file_runs_nothing_at_base(self, cases_repo, tmp_path):
        git(cases_repo, "checkout", "-q", "-b", "relay/task")
        (cases_repo / "src.txt").write_text("code only\n")
        commit_all(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        assert (run.seen_to_fail, run.seen_to_fail_log) == (None, None)

    def test_a_base_run_stopped_by_a_test_file_that_cannot_load_could_check_nothing(self, tmp_path):
        """Relay on itself: a branch adds a test file for a module that is new
        on the branch and changes one that loads at base. At base pytest
        cannot import the new file and stops before running any test, so
        every case, the loadable file's too, could not be checked."""
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / ".gitignore").write_text("reports/\n__pycache__/\n")
        write_profile(repo, f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
                            "--junitxml=reports/junit.xml tests", "test_report: reports/junit.xml\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_old.py").write_text("def test_one():\n    pass\n")
        commit_all(repo, "start")
        git(repo, "checkout", "-q", "-b", "relay/task")
        (repo / "feature.py").write_text("ANSWER = 42\n")
        (repo / "tests" / "test_feature.py").write_text(
            "import sys, os\nsys.path.insert(0, os.getcwd())\nfrom feature import ANSWER\n\n"
            "def test_answer():\n    assert ANSWER == 42\n")
        (repo / "tests" / "test_old.py").write_text("def test_one():\n    pass\n\ndef test_two():\n    pass\n")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt", base_commit=commit_of(repo))
        assert run.outcome == "passed"
        result = run.seen_to_fail
        assert (result["status"], result["failed"], result["passed"], result["did_not_run"],
                result["could_not_check"], result["reason"]) == (
            "could_not_check", 0, 0, 0, 3, STOPPED_BEFORE_TESTS)
        whys = {case["test"]: case["why"] for case in result["cases"]}
        assert whys["tests.test_old::test_one"] == whys["tests.test_old::test_two"] == STOPPED_BEFORE_TESTS
        assert whys["tests.test_feature::test_answer"].startswith("errored at base")

    def test_a_test_file_whose_name_is_not_utf8_gives_could_not_check_not_a_crash(
            self, cases_repo, tmp_path, monkeypatch):
        """Git names files in bytes. A branch's test file whose name is not
        UTF-8 cannot be given to git as text, so Seen to fail could not check,
        and the run is still recorded. (The Mac's disk refuses such a name,
        so the file goes into the commit without being written to disk.)"""
        git(cases_repo, "checkout", "-q", "-b", "relay/task")
        write_cases(cases_repo, "test_b", {"test_new": "pass"})
        git(cases_repo, "add", "-A")
        odd_name = b"tests/test_\xff.txt"
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=cases_repo, input=b"test_odd: pass\n",
                              capture_output=True, check=True).stdout.strip()
        subprocess.run([b"git", b"update-index", b"--add", b"--cacheinfo", b"100644," + blob + b"," + odd_name],
                       cwd=cases_repo, check=True)
        git(cases_repo, "commit", "-q", "-m", "a test file with an odd name")
        subprocess.run([b"git", b"update-index", b"--skip-worktree", odd_name], cwd=cases_repo, check=True)
        # A temp folder of its own, so other runs' scratch folders do not count.
        monkeypatch.setenv("TMPDIR", str(tmp_path / "temp"))
        (tmp_path / "temp").mkdir()
        monkeypatch.setattr(tempfile, "tempdir", None)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        assert run.outcome == "passed"
        assert run.seen_to_fail["status"] == "could_not_check"
        assert run.seen_to_fail["reason"] == "a changed test file's name is not UTF-8: tests/test_.txt"
        assert "Seen to fail at base: could not check (a changed test file's name is not UTF-8" in (
            evidence.summary(json.loads(json.dumps(run.to_record()))))
        assert list((tmp_path / "temp").iterdir()) == []

    def test_a_test_file_with_pathspec_magic_in_its_name_is_overlaid_literally(self, cases_repo, tmp_path):
        """Git reads a path that starts with ":(" as pathspec magic; read that
        way, ":(glob)a.test.txt" names a file that is not there and the
        overlay fails. Taken literally, it is the branch's test file."""
        git(cases_repo, "checkout", "-q", "-b", "relay/task")
        (cases_repo / ":(glob)a.test.txt").write_text("a test file by its name\n")
        write_cases(cases_repo, "test_c", {"test_plain": "pass"})
        commit_all(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        assert run.seen_to_fail["status"] == "checked"
        assert run.seen_to_fail["cases"] == [{"test": "tests.test_c::test_plain", "result": "passed",
                                              "why": None}]


class TestMatchingCasesToFiles:
    def case(self, classname="", name="t", file="", result="passed"):
        return Case(classname=classname, name=name, file=file, result=result, message="")

    @pytest.mark.parametrize("case, changed", [
        (dict(classname="tests.test_x.TestY"), "orchestrator/tests/test_x.py"),
        (dict(classname="tests.test_x"), "tests/test_x.py"),
        (dict(classname="com.a.BTest"), "src/test/java/com/a/BTest.java"),
        (dict(file="tests/test_x.py"), "orchestrator/tests/test_x.py"),
        (dict(file="./web/a.spec.ts"), "web/a.spec.ts"),
    ])
    def test_a_case_belongs_to_its_file(self, case, changed):
        assert seen_to_fail.belongs_to(self.case(**case), changed)

    @pytest.mark.parametrize("case, changed", [
        (dict(classname="tests.test_y"), "tests/test_x.py"),
        (dict(classname="tests"), "tests/test_x.py"),
        (dict(file="other.py"), "tests/test_x.py"),
        (dict(), "tests/test_x.py"),
    ])
    def test_a_case_from_another_file_does_not(self, case, changed):
        assert not seen_to_fail.belongs_to(self.case(**case), changed)

    def test_unmatched_cases_give_could_not_check(self, cases_repo, tmp_path):
        (cases_repo / "runner.py").write_text((cases_repo / "runner.py").read_text().replace(
            'classname = path[:-4].replace("/", ".")', 'classname = "elsewhere"'))
        commit_all(cases_repo, "a runner whose class names match no file")
        feature_branch(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        assert run.seen_to_fail["status"] == "could_not_check"
        assert run.seen_to_fail["reason"] == seen_to_fail.NO_CASE_MATCHED

    def test_a_skip_at_base_or_a_missing_case_did_not_run(self):
        branch = [self.case(classname="a", name="one"), self.case(classname="a", name="two")]
        base = [self.case(classname="a", name="one", result="skipped")]
        result = seen_to_fail.classify(branch, base, None, "abc")
        assert [case["result"] for case in result["cases"]] == ["did_not_run", "did_not_run"]
        assert evidence.seen_to_fail_line({"seen_to_fail": result}) == (
            "Seen to fail at base: 0 failed, 0 passed, 0 could not check, 2 did not run")


    def test_a_file_that_could_not_load_at_base_is_could_not_check(self):
        """pytest reports a test module that fails to import as one case with
        no classname, named after the module, holding <error>. It then runs
        no test at all, so test_y, which loaded, could not be checked either."""
        branch = [self.case(classname="tests.test_x", name="test_new"),
                  self.case(classname="tests.test_y", name="test_other")]
        base = [Case(classname="", name="tests.test_x", file="", result="error",
                     message="collection failure")]
        result = seen_to_fail.classify(branch, base, None, "abc",
                                       ["orchestrator/tests/test_x.py", "orchestrator/tests/test_y.py"])
        assert result["cases"] == [
            {"test": "tests.test_x::test_new", "result": "could_not_check",
             "why": "errored at base: collection failure"},
            {"test": "tests.test_y::test_other", "result": "could_not_check",
             "why": STOPPED_BEFORE_TESTS}]
        assert (result["status"], result["reason"], result["did_not_run"], result["could_not_check"]) == (
            "could_not_check", STOPPED_BEFORE_TESTS, 0, 2)
        assert evidence.seen_to_fail_line({"seen_to_fail": result}) == (
            "Seen to fail at base: 0 failed, 0 passed, 2 could not check "
            "(the run at the base stopped before any test ran)")

    def test_a_missing_case_did_not_run_when_other_tests_ran_at_base(self):
        """Only a base run where no test passed, failed or was skipped stopped
        before its tests: when one ran, a missing case is simply not there."""
        branch = [self.case(classname="tests.test_y", name="test_other")]
        base = [Case(classname="", name="tests.test_x", file="", result="error", message="collection failure"),
                Case(classname="tests.test_z", name="test_ran", file="", result="passed", message="")]
        result = seen_to_fail.classify(branch, base, None, "abc", ["tests/test_y.py"])
        assert result["cases"] == [{"test": "tests.test_y::test_other", "result": "did_not_run", "why": None}]
        assert (result["status"], result["reason"]) == ("checked", None)


class TestReadingCases:
    def test_cases_are_read_with_what_they_came_to(self, tmp_path):
        report = tmp_path / "junit.xml"
        report.write_text('<testsuites><testsuite tests="4">'
                          '<testcase classname="a" name="ok"/>'
                          '<testcase classname="a" name="bad"><failure message="boom&#10;more"/></testcase>'
                          '<testcase classname="a" name="broken"><skipped/><error>trace</error></testcase>'
                          '<testcase name="later"><skipped/></testcase>'
                          '</testsuite></testsuites>')
        assert [(case.test, case.result, case.message) for case in evidence.junit_cases(report)] == [
            ("a::ok", "passed", ""), ("a::bad", "failure", "boom"),
            ("a::broken", "error", "trace"), ("later", "skipped", "")]

    @pytest.mark.parametrize("content", [b"", b"<testsuite", b"not xml", b"<html/>", b"<testsuites/>",
                                         b'<!DOCTYPE t [<!ENTITY a "aaaa">]><testsuite tests="1"/>',
                                         b"\xff\xfe<\x00t\x00"])
    def test_a_report_parse_junit_report_refuses_gives_no_cases(self, tmp_path, content):
        report = tmp_path / "junit.xml"
        report.write_bytes(content)
        assert evidence.junit_cases(report) is None

    def test_a_report_over_the_size_limit_gives_no_cases(self, tmp_path, monkeypatch):
        monkeypatch.setattr(evidence, "MAX_REPORT_BYTES", 10)
        report = tmp_path / "junit.xml"
        report.write_text('<testsuite tests="1"><testcase name="a"/></testsuite>')
        assert evidence.junit_cases(report) is None

    def test_odd_attributes_are_read_as_text(self, tmp_path):
        report = tmp_path / "junit.xml"
        report.write_text('<testsuite tests="1"><testcase name="a" name2="x" classname="" '
                          'file="../../etc/passwd"/><testcase/></testsuite>')
        assert [(case.test, case.file) for case in evidence.junit_cases(report)] == [
            ("a", "../../etc/passwd"), ("", "")]
