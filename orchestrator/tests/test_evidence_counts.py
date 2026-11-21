"""0 tests, a missing report, or fewer tests never read as a clean pass.

Exit 0 with 0 tests run, or with no readable report where the profile
names one, is a failure and goes back to the Builder. Fewer tests run than
at the base or in the previous run stops for the owner. "Tests run" is the
report's tests minus its skipped.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import base_trust
import db
import evidence
import read_api
import recorded_runs
import run_state
import settings
from tests.git_fixtures import CASE_RUNNER, cases_repo, commit_all, commit_of, git, repo, write_cases, write_profile  # noqa: F401  (fixtures)
from tests.test_evidence import UP_TO_BUILD, current, task_in_repo
from tests.test_send_back import config, events_of_type, finish_step  # noqa: F401  (config is a fixture)

EMPTY_REPORT = "mkdir -p reports && echo '<testsuite tests=\"0\" failures=\"0\" errors=\"0\" skipped=\"0\"/>' > reports/junit.xml"


def on_branch(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "relay/task")


def two_of_three(repo: Path) -> None:
    write_cases(repo, "test_a", {"test_one": "pass", "test_two": "pass"})
    commit_all(repo)


class TestNoTestsOrNoReport:
    def test_exit_0_with_0_tests_goes_back_to_the_builder(self, config, repo):
        write_profile(repo, EMPTY_REPORT, "test_report: reports/junit.xml\n")
        commit_all(repo)
        task_in_repo("empty", config, repo, UP_TO_BUILD)
        finish_step("empty", config, "ready-for-review")
        assert commands.cmd_next("empty", auto=False, config=config) == 0
        status = read_api.status_payload("empty", config)
        run = status["test_runs"][-1]
        assert (run["outcome"], run["exit_code"], run["tests_run"]) == ("failed", 0, 0)
        assert run["reason"].startswith("0 tests ran")
        assert current("empty", config) == ("in_progress", "builder")
        assert status["retries"]["builder"] == 1
        assert "Tests FAILED (0 tests ran" in evidence.summary(run)

    def test_a_named_report_that_was_not_written_goes_back_to_the_builder(self, config, repo):
        write_profile(repo, "echo no report", "test_report: test-results/junit.xml\n")
        (repo / ".gitignore").write_text("reports/\ntest-results/\n")
        commit_all(repo)
        task_in_repo("no-report", config, repo, UP_TO_BUILD)
        finish_step("no-report", config, "ready-for-review")
        assert commands.cmd_next("no-report", auto=False, config=config) == 0
        run = read_api.status_payload("no-report", config)["test_runs"][-1]
        assert (run["outcome"], run["exit_code"]) == ("failed", 0)
        assert run["reason"].startswith(
            "The profile names test-results/junit.xml, but this run did not write a readable report")
        assert current("no-report", config) == ("in_progress", "builder")

    def test_an_unreadable_report_is_the_same_as_none(self, repo, tmp_path):
        write_profile(repo, "mkdir -p reports && echo 'not xml' > reports/junit.xml",
                      "test_report: reports/junit.xml\n")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert (run.outcome, run.exit_code, run.counts) == ("failed", 0, None)
        assert "did not write a readable report" in run.reason

    def test_no_report_named_routes_as_today_and_says_counts_were_not_checked(self, config, repo):
        task_in_repo("unnamed", config, repo, UP_TO_BUILD)
        finish_step("unnamed", config, "ready-for-review")
        assert commands.cmd_next("unnamed", auto=False, config=config) == 0
        run = read_api.status_payload("unnamed", config)["test_runs"][-1]
        assert (run["outcome"], run["counts_unchecked"]) == ("passed", "the profile names no test report")
        assert ("counts unknown (the profile names no test report), so Relay could not check for 0 or "
                "fewer tests") in evidence.summary(run)
        assert current("unnamed", config) == ("in_progress", "reviewer")

    def test_a_failing_run_with_no_tests_keeps_its_exit_code_reason(self, repo, tmp_path):
        write_profile(repo, EMPTY_REPORT + "; exit 4", "test_report: reports/junit.xml\n")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert (run.outcome, run.exit_code, run.reason) == ("failed", 4, None)
        assert "FAILED (exit code 4)" in evidence.summary(run.to_record())


class TestFewerTests:
    def test_fewer_tests_than_the_base_stops_for_the_owner(self, config, cases_repo, tmp_path, monkeypatch,
                                                            capsys):
        folders = tmp_path / "folders.txt"
        monkeypatch.setenv("RUNNER_CWD_LOG", str(folders))
        task_in_repo("fewer", config, cases_repo, UP_TO_BUILD)
        on_branch(cases_repo)
        two_of_three(cases_repo)
        finish_step("fewer", config, "ready-for-review")
        capsys.readouterr()
        assert commands.cmd_next("fewer", auto=False, config=config) == 0
        out = capsys.readouterr().out
        assert "  This task changed how the repo is tested.\n  1 fewer tests ran than at the base (3 before, 2 now)\n" in out
        assert "  Approving accepts what is listed above for this task; a further change asks again.\n" in out
        run = read_api.status_payload("fewer", config)["test_runs"][-1]
        assert (run["outcome"], run["tests_run"], run["base_tests_run"]) == ("passed", 2, 3)
        assert run["tests_drop"] == {"fewer": 1, "reference": "base", "before": 3, "now": 2}
        gate = read_api.gate_check_payload("fewer", config)["gate"]
        assert (gate["state"], gate["kind"]) == ("waiting_on_owner", "approval")
        assert "1 fewer tests ran than at the base (3 before, 2 now)" in gate["reason"]
        assert (settings.TASKS_DIR / "fewer" / "03-test-run-base.log").exists()
        ran_in = folders.read_text().splitlines()
        base_folder = next(Path(folder) for folder in ran_in if Path(folder) != cases_repo.resolve()
                           and Path(folder) != cases_repo)
        assert cases_repo.resolve() not in base_folder.resolve().parents
        assert base_folder.name.startswith("relay-scratch-")
        assert not base_folder.exists()

    def test_new_skips_count_as_a_drop(self, cases_repo, tmp_path):
        on_branch(cases_repo)
        write_cases(cases_repo, "test_a", {"test_one": "pass", "test_two": "pass", "test_three": "skip"})
        commit_all(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        assert (run.outcome, run.counts["tests"], run.tests_run) == ("passed", 3, 2)
        assert run.tests_drop == {"fewer": 1, "reference": "base", "before": 3, "now": 2}

    def test_fewer_than_the_previous_run_stops_and_the_larger_drop_is_named(self, cases_repo, tmp_path):
        on_branch(cases_repo)
        two_of_three(cases_repo)
        earlier = [{"outcome": "passed", "tests_run": 2},
                   {"outcome": "failed", "counts": {"tests": 6, "failures": 1, "errors": 0, "skipped": 0}},
                   {"outcome": "uncommitted", "tests_run": 50}]
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo), earlier_runs=earlier)
        assert (run.previous_tests_run, run.base_tests_run) == (6, 3)
        assert run.tests_drop == {"fewer": 4, "reference": "previous run", "before": 6, "now": 2}
        assert "4 fewer tests ran than the previous run (6 before, 2 now)" in evidence.confirmation_reason(
            run.to_record())

    def test_more_tests_than_before_flag_nothing(self, cases_repo, tmp_path):
        on_branch(cases_repo)
        write_cases(cases_repo, "test_b", {"test_four": "pass"})
        commit_all(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo),
                                 earlier_runs=[{"outcome": "passed", "tests_run": 3}])
        assert (run.tests_run, run.base_tests_run, run.tests_drop) == (4, 3, None)

    def test_an_unknown_base_count_skips_only_the_base_comparison(self, cases_repo, tmp_path):
        write_profile(cases_repo, "python3 runner.py")
        commit_all(cases_repo, "the base names no report")
        on_branch(cases_repo)
        write_profile(cases_repo, "python3 runner.py", "test_report: reports/junit.xml\n")
        two_of_three(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo),
                                 earlier_runs=[{"outcome": "passed", "tests_run": 3}])
        assert (run.base_tests_run, run.base_count_unknown) == (None, "the base's profile names no test report")
        assert run.tests_drop == {"fewer": 1, "reference": "previous run", "before": 3, "now": 2}
        assert "The base's test count is unknown (the base's profile names no test report)." in evidence.summary(
            run.to_record())

    def test_a_base_run_that_writes_no_report_leaves_the_base_count_unknown(self, cases_repo, tmp_path):
        (cases_repo / "runner.py").write_text("print('no report at the base')\n")
        commit_all(cases_repo, "a base that writes no report")
        on_branch(cases_repo)
        (cases_repo / "runner.py").write_text(CASE_RUNNER)
        commit_all(cases_repo)
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo))
        assert run.base_count_unknown == "the run at the base wrote no readable report"
        assert run.tests_drop is None

    def test_the_base_is_measured_once_per_fork_commit(self, config, cases_repo):
        task_in_repo("once", config, cases_repo, UP_TO_BUILD)
        on_branch(cases_repo)
        write_cases(cases_repo, "test_b", {"test_four": "pass"})
        commit_all(cases_repo)
        state = run_state.load_run_state("once", config)
        assert base_trust.check_before_step("once", state, config) is None   # as every step does
        first = recorded_runs.ensure_test_run("once", state, "builder", "ready-for-review", 3, config)
        base_log = settings.TASKS_DIR / "once" / "03-test-run-base.log"
        assert first["base_tests_run"] == 3 and base_log.exists()
        base_log.unlink()
        write_cases(cases_repo, "test_c", {"test_five": "pass"})
        commit_all(cases_repo)
        second = recorded_runs.ensure_test_run("once", state, "builder", "ready-for-review", 5, config)
        assert (second["base_tests_run"], second["base_commit"]) == (3, first["base_commit"])
        assert not base_log.exists()
        assert not (settings.TASKS_DIR / "once" / "05-test-run-base.log").exists()

    def test_a_forged_earlier_count_is_passed_over(self, cases_repo, tmp_path):
        """Earlier runs come from the ledger; a value of the wrong type in
        one is not a count."""
        on_branch(cases_repo)
        two_of_three(cases_repo)
        fork = evidence.fork_point(cases_repo, commit_of(cases_repo))
        earlier = [{"outcome": "passed", "tests_run": True, "counts": "lots"},
                   {"outcome": "passed", "base_commit": fork, "base_tests_run": "3"}]
        run = evidence.run_tests(cases_repo, tmp_path / "log.txt", base_commit=commit_of(cases_repo), earlier_runs=earlier)
        assert run.previous_tests_run is None
        assert run.base_tests_run == 3 and run.base_log is not None


class TestTheOwnersAcceptanceOfACount:
    def test_an_accepted_count_covers_the_same_or_more_and_fewer_asks_again(self, config, cases_repo):
        task_in_repo("count-ok", config, cases_repo, UP_TO_BUILD)
        on_branch(cases_repo)
        two_of_three(cases_repo)
        finish_step("count-ok", config, "ready-for-review")
        assert commands.cmd_next("count-ok", auto=False, config=config) == 0
        assert commands.cmd_override("count-ok", "reviewer", config) == 0
        accepted = events_of_type("count-ok", db.EVENT_TEST_COMMAND_ACCEPTED)
        assert accepted[0]["payload"]["tests_run"] == 2
        assert "harness_digest" not in accepted[0]["payload"]

        finish_step("count-ok", config, "approved")
        assert commands.cmd_next("count-ok", auto=False, config=config) == 0
        (cases_repo / "src.txt").write_text("same tests\n")
        commit_all(cases_repo)
        finish_step("count-ok", config, "pass")
        assert commands.cmd_next("count-ok", auto=False, config=config) == 0
        assert current("count-ok", config) == ("in_progress", "security")

    def test_fewer_than_the_accepted_count_asks_again(self, config, cases_repo):
        task_in_repo("count-again", config, cases_repo, UP_TO_BUILD)
        on_branch(cases_repo)
        two_of_three(cases_repo)
        finish_step("count-again", config, "ready-for-review")
        assert commands.cmd_next("count-again", auto=False, config=config) == 0
        assert commands.cmd_override("count-again", "reviewer", config) == 0
        finish_step("count-again", config, "approved")
        assert commands.cmd_next("count-again", auto=False, config=config) == 0
        write_cases(cases_repo, "test_a", {"test_one": "pass"})
        commit_all(cases_repo)
        finish_step("count-again", config, "pass")
        assert commands.cmd_next("count-again", auto=False, config=config) == 0
        gate = read_api.gate_check_payload("count-again", config)["gate"]
        assert gate["state"] == "waiting_on_owner"
        assert "2 fewer tests ran than at the base (3 before, 1 now)" in gate["reason"]

    def test_a_forged_acceptance_count_covers_nothing(self):
        assert recorded_runs._covers_count("2", 3) is False
        assert recorded_runs._covers_count(True, 3) is False
        assert recorded_runs._covers_count(None, 3) is False
        assert recorded_runs._covers_count(3, 3) is True
