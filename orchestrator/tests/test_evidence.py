"""Tests for test evidence: Relay runs a repo's tests itself and records the run.

A "ready" verdict from the Builder or the Tester is checked against Relay's
own run of the profile's test command. A run that fails, times out, or finds
uncommitted work sends the task back to the Builder.
"""

import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import evidence
import settings
import run_state
import routing
import recorded_runs
import read_api
import commands
import project_profile
from project_profile import check_profile, load_profile
from tests.hostile_yaml import (  # noqa: F401  (nothing_composed is a fixture)
    BAD_VALUE_NAMES, BAD_VALUES, SECURITYS_MERGE_KEYS, nothing_composed,
)
from tests.test_send_back import UP_TO_REVIEW, config, events_of_type, finish_step, task_at  # noqa: F401  (config is a fixture)

RUN_KEYS = {"outcome", "reason", "commit", "command", "test_report", "command_differs_from_base", "base_command",
            "base_profile_unread", "exit_code", "duration_seconds", "counts", "log"}

JUNIT = ('<testsuites><testsuite tests="5" failures="1" errors="0" skipped="2"/>'
         '<testsuite tests="3" failures="0" errors="1" skipped="0"/></testsuites>')


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def write_profile(repo: Path, test_command: str, extra: str = "") -> None:
    (repo / ".relay").mkdir(exist_ok=True)
    (repo / ".relay" / "profile.yaml").write_text(
        f"schema_version: 1\ncommands:\n  test: {json.dumps(test_command)}\n{extra}")


def commit_all(repo: Path, message: str = "work") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


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


class TestParseJunitReport:
    def test_sums_the_suites_in_a_wrapper(self, tmp_path):
        report = tmp_path / "junit.xml"
        report.write_text(JUNIT)
        assert evidence.parse_junit_report(report) == {
            "tests": 8, "failures": 1, "errors": 1, "skipped": 2}

    def test_reads_a_single_suite_and_the_disabled_spelling(self, tmp_path):
        report = tmp_path / "junit.xml"
        report.write_text('<testsuite tests="4" failures="0" errors="0" disabled="1"/>')
        assert evidence.parse_junit_report(report) == {
            "tests": 4, "failures": 0, "errors": 0, "skipped": 1}

    @pytest.mark.parametrize("content", ["not xml", "<html/>", "<testsuites/>",
                                         '<testsuite tests="many"/>'])
    def test_anything_else_gives_no_counts(self, tmp_path, content):
        report = tmp_path / "junit.xml"
        report.write_text(content)
        assert evidence.parse_junit_report(report) is None

    def test_a_report_with_a_document_type_is_refused(self, tmp_path):
        report = tmp_path / "junit.xml"
        report.write_text('<!DOCTYPE t [<!ENTITY a "aaaa">]><testsuite tests="1"/>')
        assert evidence.parse_junit_report(report) is None

    def test_missing_file_gives_no_counts(self, tmp_path):
        assert evidence.parse_junit_report(tmp_path / "absent.xml") is None


class TestRunTests:
    def test_passing_run_records_commit_command_and_log(self, repo, tmp_path):
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert set(run) == RUN_KEYS
        assert (run["outcome"], run["exit_code"], run["command"]) == ("passed", 0, "echo all good")
        assert run["commit"] == git(repo, "rev-parse", "HEAD")
        assert run["counts"] is None
        assert (tmp_path / "log.txt").read_text() == "all good\n"

    def test_failing_run_keeps_the_exit_code_and_output(self, repo, tmp_path):
        write_profile(repo, "echo boom; exit 3")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert (run["outcome"], run["exit_code"]) == ("failed", 3)
        assert "boom" in evidence.log_tail(tmp_path / "log.txt")
        assert "FAILED (exit code 3)" in evidence.summary(run)

    def test_a_run_past_the_time_limit_is_stopped(self, repo, tmp_path):
        write_profile(repo, "sleep 30")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt", timeout_seconds=1)
        assert (run["outcome"], run["exit_code"]) == ("timed_out", None)
        assert run["duration_seconds"] < 10

    def test_uncommitted_work_is_not_tested(self, repo, tmp_path):
        marker = tmp_path / "ran"
        write_profile(repo, f"touch {marker}")
        commit_all(repo)
        (repo / "new_file.py").write_text("x = 1\n")
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert run["outcome"] == "uncommitted"
        assert "new_file.py" in run["reason"]
        assert not marker.exists()

    def test_no_profile_or_a_bad_one_is_not_run(self, repo, tmp_path):
        (repo / ".relay" / "profile.yaml").write_text("schema_version: 1\n")
        commit_all(repo)
        assert evidence.run_tests(repo, tmp_path / "log.txt")["outcome"] == "not_run"
        git(repo, "rm", "-q", ".relay/profile.yaml")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert (run["outcome"], run["command"]) == ("not_run", None)
        assert "no project profile" in run["reason"]

    def test_counts_come_from_the_report_this_run_wrote(self, repo, tmp_path):
        write_profile(repo, f"mkdir -p reports && echo '{JUNIT}' > reports/junit.xml",
                      "test_report: reports/junit.xml\n")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert run["counts"] == {"tests": 8, "failures": 1, "errors": 1, "skipped": 2}
        assert "8 tests, 2 failed, 2 skipped" in evidence.summary(run)

    def test_a_report_left_over_from_an_earlier_run_is_ignored(self, repo, tmp_path):
        (repo / "reports").mkdir()
        stale = repo / "reports" / "junit.xml"
        stale.write_text(JUNIT)
        os.utime(stale, (1_000_000_000, 1_000_000_000))
        write_profile(repo, "true", "test_report: reports/junit.xml\n")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert (run["outcome"], run["counts"]) == ("passed", None)
        assert "counts unknown" in evidence.summary(run)

    def test_a_changed_test_command_is_flagged(self, repo, tmp_path):
        assert evidence.run_tests(repo, tmp_path / "log.txt", base="main")[
            "command_differs_from_base"] is False
        git(repo, "checkout", "-q", "-b", "relay/task")
        write_profile(repo, "true")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt", base="main")
        assert run["command_differs_from_base"] is True
        assert "is not the base branch's" in evidence.summary(run)

    def test_a_changed_report_path_or_a_base_without_a_profile_is_flagged_too(self, repo, tmp_path):
        git(repo, "checkout", "-q", "-b", "relay/task")
        write_profile(repo, "echo all good", "test_report: reports/junit.xml\n")
        commit_all(repo)
        assert evidence.run_tests(repo, tmp_path / "log.txt", base="main")[
            "command_differs_from_base"] is True
        assert evidence.run_tests(repo, tmp_path / "log.txt", base="no-such-branch")[
            "command_differs_from_base"] is True
        assert evidence.run_tests(repo, tmp_path / "log.txt", base=None)[
            "command_differs_from_base"] is False

    def test_a_run_that_leaves_files_behind_is_not_accepted(self, repo, tmp_path):
        write_profile(repo, "echo junk > leftover.txt")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert (run["outcome"], run["exit_code"]) == ("uncommitted", 0)
        assert "leftover.txt" in run["reason"] and ".gitignore" in run["reason"]

    def test_a_failed_run_that_leaves_files_behind_is_still_a_failure(self, repo, tmp_path):
        write_profile(repo, "echo junk > leftover.txt; exit 2")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert (run["outcome"], run["exit_code"]) == ("failed", 2)
        assert "leftover.txt" in run["reason"]

    def test_a_branch_that_removes_or_breaks_its_bases_profile_is_not_let_off(self, repo, tmp_path):
        git(repo, "checkout", "-q", "-b", "relay/task")
        (repo / ".relay" / "profile.yaml").write_text("schema_version: 1\ncomands: {}\n")
        commit_all(repo)
        broken = evidence.run_tests(repo, tmp_path / "log.txt", base="main")
        assert (broken["outcome"], broken["base_command"]) == ("profile_broken", "echo all good")
        assert "profile with errors" in broken["reason"]
        git(repo, "rm", "-q", ".relay/profile.yaml")
        commit_all(repo)
        removed = evidence.run_tests(repo, tmp_path / "log.txt", base="main")
        assert removed["outcome"] == "profile_broken"
        assert "has no project profile" in removed["reason"]
        assert removed["outcome"] in evidence.FAILING_OUTCOMES

    def test_a_profile_change_on_the_base_after_the_task_started_is_not_flagged(self, repo, tmp_path):
        git(repo, "checkout", "-q", "-b", "relay/task")
        (repo / "work.py").write_text("x = 1\n")
        commit_all(repo)
        git(repo, "checkout", "-q", "main")
        write_profile(repo, "echo newer command on main")
        commit_all(repo)
        git(repo, "checkout", "-q", "relay/task")
        run = evidence.run_tests(repo, tmp_path / "log.txt", base="main")
        assert (run["command_differs_from_base"], run["base_command"]) == (False, "echo all good")

    @pytest.mark.parametrize("hostile, reason", list(zip([SECURITYS_MERGE_KEYS, *BAD_VALUES], [
        "it holds a YAML anchor, alias or merge key (&name, *name or an unquoted <<).",
        "it holds a value YAML cannot build, such as the date 2026-99-99.",
        "it holds a value YAML cannot build, such as the date 2026-99-99.",
        "it holds a YAML tag (such as !!str).",
        "it holds a YAML tag (such as !!str).",
    ], strict=True)), ids=["Security's merge keys", *BAD_VALUE_NAMES])
    def test_a_base_profile_relay_does_not_read_gives_no_base_command_and_never_raises(
            self, repo, tmp_path, nothing_composed, hostile, reason):
        """Hostile YAML in the reader of the profile at the task's base.
        With no base command the branch's is flagged as not the base's, for the owner to confirm."""
        write_profile(repo, "echo all good", hostile)
        commit_all(repo, "a base profile Relay does not read")
        git(repo, "checkout", "-q", "-b", "relay/task")
        write_profile(repo, "echo all good")
        commit_all(repo)
        started = time.monotonic()
        assert evidence.base_profile(repo, "main") == (None, reason)
        assert time.monotonic() - started < 2
        run = evidence.run_tests(repo, tmp_path / "log.txt", base="main")
        assert (run["outcome"], run["command_differs_from_base"]) == ("passed", True)

    def test_a_base_profile_that_is_not_utf8_gives_no_base_command_and_never_raises(self, repo):
        """git's answer is decoded with room for bytes that are not UTF-8,
        so such a profile does not end the run."""
        (repo / ".relay" / "profile.yaml").write_bytes(
            b'schema_version: 1\ncommands:\n  test: "echo all good"\n# \xff\n')
        commit_all(repo, "a base profile that is not UTF-8")
        assert evidence.base_profile(repo, "main") == (None, "it is not UTF-8.")

    def test_a_base_profile_over_the_size_limit_is_never_shown_or_parsed(self, repo, monkeypatch):
        write_profile(repo, "echo all good", "#" + "x" * project_profile.MAX_PROFILE_BYTES + "\n")
        commit_all(repo, "a base profile over the limit")
        asked, parsed = [], []
        real_git = evidence._git
        monkeypatch.setattr(evidence, "_git", lambda worktree, *args: asked.append(args[0])
                            or real_git(worktree, *args))
        monkeypatch.setattr(project_profile.bounded_yaml.yaml, "parse",
                            lambda text, **options: parsed.append(len(text)) or iter(()))
        assert evidence.base_profile(repo, "main") == (None, "it is larger than 64 KB.")
        assert (asked, parsed) == (["merge-base", "cat-file"], [])

    def test_a_run_that_moves_the_branch_is_not_accepted(self, repo, tmp_path):
        write_profile(repo, "git -c user.name=T -c user.email=t@example.com commit -q --allow-empty -m sneaky")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt")
        assert run["outcome"] == "uncommitted"
        assert "different commit" in run["reason"]


class TestProfileTestReport:
    def profile(self, report) -> dict:
        return {"schema_version": 1, "commands": {"test": "pytest"}, "test_report": report}

    def test_a_path_inside_the_repo_is_accepted(self):
        assert check_profile(self.profile("reports/junit.xml")) == ([], [])

    @pytest.mark.parametrize("report", ["/tmp/junit.xml", "../junit.xml", "a/../../junit.xml"])
    def test_a_path_outside_the_repo_is_an_error(self, report):
        assert check_profile(self.profile(report))[0] == [
            "test_report must be a path inside the repo (no leading '/' and no '..')."]

    @pytest.mark.parametrize("report", ["", None, ["junit.xml"]])
    def test_it_must_be_text(self, report):
        assert "test_report must be text" in check_profile(self.profile(report))[0][0]

    def test_a_report_git_does_not_ignore_is_a_warning(self, repo):
        write_profile(repo, "true", "test_report: results/junit.xml\n")
        assert ["test_report is not in .gitignore" in warning
                for warning in load_profile(repo).warnings] == [True]
        write_profile(repo, "true", "test_report: reports/junit.xml\n")
        assert load_profile(repo).warnings == []


def task_in_repo(task_id: str, config: dict, repo: Path, verdicts) -> None:
    """Like task_at, with the task's worktree pointed at `repo` from the start."""
    task_at(task_id, config, [])
    state = run_state.load_run_state(task_id, config)
    state["worktree_path"], state["base"] = str(repo), "main"
    run_state.save_run_state(task_id, state, config)
    for verdict, extra_frontmatter in verdicts:
        finish_step(task_id, config, verdict, extra_frontmatter)
        commands.cmd_next(task_id, auto=False, config=config)


UP_TO_BUILD = UP_TO_REVIEW[:-1]


def current(task_id: str, config: dict) -> tuple[str, str]:
    task = read_api.status_payload(task_id, config)["task"]
    return task["status"], task["current_phase"]


class TestRelayNextRunsTheTests:
    def test_passing_tests_let_the_builder_through_and_are_recorded(self, config, repo, capsys):
        task_in_repo("green", config, repo, UP_TO_REVIEW)
        out = capsys.readouterr().out
        assert current("green", config) == ("in_progress", "reviewer")
        runs = read_api.status_payload("green", config)["test_runs"]
        assert [set(run) for run in runs] == [RUN_KEYS | {"phase", "step", "timestamp"}]
        assert (runs[0]["phase"], runs[0]["step"], runs[0]["outcome"]) == ("builder", 3, "passed")
        assert runs[0]["commit"] == git(repo, "rev-parse", "HEAD")
        assert (settings.TASKS_DIR / "green" / "03-test-run.log").read_text() == "all good\n"
        assert "Relay's recorded test run: Tests passed at" in out

    def test_failing_tests_send_the_builder_back_with_the_output(self, config, repo, capsys):
        write_profile(repo, "echo assertion blew up; exit 1")
        commit_all(repo)
        task_in_repo("red", config, repo, UP_TO_REVIEW)
        out = capsys.readouterr().out
        status = read_api.status_payload("red", config)
        assert current("red", config) == ("in_progress", "builder")
        assert status["retries"]["builder"] == 1
        assert status["latest_step"]["artifact"] == "04-builder-fix-1.md"
        assert status["test_runs"][0]["outcome"] == "failed"
        assert "Relay's own test run did not pass (failed); back to the builder" in out
        assert "This step exists because that run did not pass." in out
        assert "assertion blew up" in out

    def test_uncommitted_work_sends_the_builder_back(self, config, repo):
        task_in_repo("dirty", config, repo, UP_TO_BUILD)
        (repo / "forgot_to_commit.py").write_text("x = 1\n")
        finish_step("dirty", config, "ready-for-review")
        commands.cmd_next("dirty", auto=False, config=config)
        assert current("dirty", config) == ("in_progress", "builder")
        assert read_api.status_payload("dirty", config)["test_runs"][0]["outcome"] == "uncommitted"

    def test_a_tester_pass_is_overruled_by_a_failing_run(self, config, repo):
        task_in_repo("late", config, repo, UP_TO_REVIEW + [("approved", "")])
        write_profile(repo, "exit 1")
        commit_all(repo)
        finish_step("late", config, "pass")
        commands.cmd_next("late", auto=False, config=config)
        assert current("late", config) == ("in_progress", "builder")
        outcomes = [(run["phase"], run["outcome"])
                    for run in read_api.status_payload("late", config)["test_runs"]]
        assert outcomes == [("builder", "passed"), ("tester", "failed")]

    def test_failing_runs_stop_at_the_retry_limit(self, config, repo):
        write_profile(repo, "exit 1")
        commit_all(repo)
        task_in_repo("stuck", config, repo, UP_TO_REVIEW + [("ready-for-review", "")] * 3)
        status = read_api.status_payload("stuck", config)
        assert status["task"]["status"] == "blocked"
        assert status["retries"]["builder"] == 3
        assert len(status["test_runs"]) == 4

    def test_a_repo_without_a_profile_carries_on_and_says_so(self, config, repo, capsys):
        git(repo, "rm", "-q", ".relay/profile.yaml")
        commit_all(repo)
        task_in_repo("bare", config, repo, UP_TO_REVIEW)
        out = capsys.readouterr().out
        assert current("bare", config) == ("in_progress", "reviewer")
        assert read_api.status_payload("bare", config)["test_runs"][0]["outcome"] == "not_run"
        assert "run the tests yourself and say so" in out

    def test_a_task_with_no_working_directory_carries_on_and_says_so(self, config):
        task_at("nowhere", config, UP_TO_REVIEW)
        assert current("nowhere", config) == ("in_progress", "reviewer")
        runs = read_api.status_payload("nowhere", config)["test_runs"]
        assert [(run["outcome"], run["step"]) for run in runs] == [("not_run", 3)]
        assert "no working directory" in runs[0]["reason"]

    def test_the_same_commit_is_not_tested_twice(self, config, repo):
        task_in_repo("once", config, repo, UP_TO_BUILD)
        state = run_state.load_run_state("once", config)
        first = recorded_runs.ensure_test_run("once", state, "builder", "ready-for-review", 3, config)
        again = recorded_runs.ensure_test_run("once", state, "builder", "ready-for-review", 3, config)
        assert first == again
        assert len(recorded_runs.read_test_runs("once", config)) == 1

        (repo / "more.py").write_text("y = 2\n")
        commit_all(repo)
        recorded_runs.ensure_test_run("once", state, "builder", "ready-for-review", 3, config)
        assert len(recorded_runs.read_test_runs("once", config)) == 2

    def test_other_verdicts_run_no_tests(self, config, repo):
        task_in_repo("early", config, repo, UP_TO_BUILD)
        assert recorded_runs.read_test_runs("early", config) == []

    def test_status_shows_the_runs(self, config, repo, capsys):
        task_in_repo("shown", config, repo, UP_TO_REVIEW)
        capsys.readouterr()
        commands.cmd_status("shown", config)
        assert "after step 3 (builder): Tests passed at" in capsys.readouterr().out


def task_on_branch(task_id: str, config: dict, repo: Path, test_command: str) -> None:
    """A task at the Builder step whose branch changed the test command."""
    task_in_repo(task_id, config, repo, UP_TO_BUILD)
    git(repo, "checkout", "-q", "-b", f"relay/{task_id}")
    write_profile(repo, test_command)
    commit_all(repo)
    finish_step(task_id, config, "ready-for-review")
    commands.cmd_next(task_id, auto=False, config=config)


class TestAChangedTestCommandNeedsTheOwner:
    def test_a_pass_with_a_changed_command_stops_for_the_owner(self, config, repo, capsys):
        task_on_branch("swap", config, repo, "true")
        out = capsys.readouterr().out
        assert current("swap", config) == ("in_progress", "builder")
        gate = read_api.gate_check_payload("swap", config)["gate"]
        assert (gate["state"], gate["kind"], gate["next_phase"]) == (
            "waiting_on_owner", "approval", "reviewer")
        assert "changed the test command" in gate["reason"]
        assert gate["approve_command"] == f"{settings._CLI_CMD} override swap reviewer"
        assert "HUMAN GATE" in out

    def test_one_approval_covers_the_rest_of_the_task(self, config, repo, capsys):
        task_on_branch("swap-ok", config, repo, "true")
        commands.cmd_override("swap-ok", "reviewer", config)
        assert "Relay's recorded test run: Tests passed" in capsys.readouterr().out
        for verdict in ("approved", "pass"):
            finish_step("swap-ok", config, verdict)
            commands.cmd_next("swap-ok", auto=False, config=config)
        assert current("swap-ok", config) == ("in_progress", "security")

    def test_changing_the_command_again_asks_again(self, config, repo):
        task_on_branch("swap-twice", config, repo, "true")
        commands.cmd_override("swap-twice", "reviewer", config)
        finish_step("swap-twice", config, "approved")
        commands.cmd_next("swap-twice", auto=False, config=config)
        write_profile(repo, "exit 0")
        commit_all(repo)
        finish_step("swap-twice", config, "pass")
        commands.cmd_next("swap-twice", auto=False, config=config)
        assert current("swap-twice", config) == ("in_progress", "tester")
        assert read_api.gate_check_payload("swap-twice", config)["gate"]["state"] == "waiting_on_owner"

    def test_an_override_to_another_phase_accepts_nothing(self, config, repo):
        task_on_branch("swap-other", config, repo, "true")
        commands.cmd_override("swap-other", "builder", config)
        assert events_of_type("swap-other", db.EVENT_TEST_COMMAND_ACCEPTED) == []
        finish_step("swap-other", config, "ready-for-review")
        commands.cmd_next("swap-other", auto=False, config=config)
        assert read_api.gate_check_payload("swap-other", config)["gate"]["state"] == "waiting_on_owner"

    def test_the_acceptance_is_recorded_with_the_exact_command(self, config, repo, capsys):
        task_on_branch("swap-exact", config, repo, "true")
        out = capsys.readouterr().out
        assert "This task changed how the repo is tested." in out
        assert "The base branch's test command: echo all good" in out
        commands.cmd_override("swap-exact", "reviewer", config)
        accepted = events_of_type("swap-exact", db.EVENT_TEST_COMMAND_ACCEPTED)
        assert [(event["phase"], event["step"]) for event in accepted] == [("builder", 3)]
        assert accepted[0]["payload"] == {"command": "true", "test_report": None,
                                          "commit": git(repo, "rev-parse", "HEAD")}

    @pytest.mark.parametrize("base_profile, reason", [
        (b'schema_version: 1\ncommands:\n  test: "echo all good"\nreleased: 2026-99-99\n',
         "it holds a value YAML cannot build, such as the date 2026-99-99."),
        (SECURITYS_MERGE_KEYS.encode(),
         "it holds a YAML anchor, alias or merge key (&name, *name or an unquoted <<)."),
        (b"#" + b"x" * project_profile.MAX_PROFILE_BYTES + b"\n", "it is larger than 64 KB."),
        (b'schema_version: 1\ncommands:\n  test: "echo all good"\n# \xff\n', "it is not UTF-8."),
    ], ids=["a date that does not exist", "Security's merge keys", "over the size limit", "not UTF-8"])
    def test_a_base_profile_relay_refused_is_said_with_its_reason_beside_the_base_command(
            self, config, repo, capsys, base_profile, reason):
        """docs/profile.md says a refused base profile is reported as one
        Relay could not read."""
        (repo / ".relay" / "profile.yaml").write_bytes(base_profile)
        commit_all(repo, "a base profile Relay refuses")
        task_on_branch("refused-base", config, repo, "true")
        out = capsys.readouterr().out
        assert "  The base branch's test command: (none)\n" in out
        assert f"  The base branch's profile could not be read: {reason}\n" in out

    def test_a_base_profile_that_is_not_yaml_is_said_in_one_line(self, config, repo, capsys):
        """YAML's own error takes several lines; the reason is given in one."""
        (repo / ".relay" / "profile.yaml").write_text("schema_version: 1\ncommands: [\n  test\n")
        commit_all(repo, "a base profile that is not YAML")
        task_on_branch("not-yaml-base", config, repo, "true")
        said = [line for line in capsys.readouterr().out.splitlines() if evidence.BASE_PROFILE_UNREAD in line]
        assert len(said) == 1
        assert said[0].startswith(f"  {evidence.BASE_PROFILE_UNREAD}: while parsing a flow sequence")
        assert "but got '<stream end>'" in said[0]

    def test_a_base_profile_reason_is_printed_without_invisible_format_characters(
            self, config, repo, capsys):
        """YAML's error quotes the profile's line,
        and a right-to-left override in it could reorder the printed line."""
        (repo / ".relay" / "profile.yaml").write_text(
            "schema_version: 1\ncommands: [‮evil​﻿\n  test\n", encoding="utf-8")
        commit_all(repo, "a base profile that is not YAML and holds format characters")
        task_on_branch("format-characters-base", config, repo, "true")
        said = [line for line in capsys.readouterr().out.splitlines() if evidence.BASE_PROFILE_UNREAD in line]
        assert len(said) == 1
        assert said[0].startswith(f"  {evidence.BASE_PROFILE_UNREAD}: while parsing a flow sequence")
        assert "evil" in said[0]
        assert [character for character in said[0] if unicodedata.category(character) == "Cf"] == []

    def test_a_base_with_no_profile_gets_no_line_on_its_profile(self, config, repo, capsys):
        git(repo, "rm", "-q", ".relay/profile.yaml")
        commit_all(repo, "no profile at the base")
        task_on_branch("no-base-profile", config, repo, "true")
        out = capsys.readouterr().out
        assert "  The base branch's test command: (none)\n" in out
        assert evidence.BASE_PROFILE_UNREAD not in out

    def test_the_profile_document_names_the_line_relay_next_prints(self):
        profile_doc = Path(__file__).resolve().parents[2] / "docs" / "profile.md"
        assert f'"{evidence.BASE_PROFILE_UNREAD}:"' in profile_doc.read_text(encoding="utf-8")

    def test_an_approval_at_another_gate_accepts_nothing(self, config, repo):
        """An approval the owner gave for something else must not count."""
        task_in_repo("swap-later", config, repo, UP_TO_REVIEW)
        finish_step("swap-later", config, "approved_with_warnings")
        commands.cmd_next("swap-later", auto=False, config=config)
        commands.cmd_override("swap-later", "tester", config)
        git(repo, "checkout", "-q", "-b", "relay/swap-later")
        write_profile(repo, "true")
        commit_all(repo)
        finish_step("swap-later", config, "pass")
        commands.cmd_next("swap-later", auto=False, config=config)
        assert events_of_type("swap-later", db.EVENT_TEST_COMMAND_ACCEPTED) == []
        assert read_api.gate_check_payload("swap-later", config)["gate"]["state"] == "waiting_on_owner"

    def test_a_branch_that_deletes_the_profile_goes_back_to_the_builder(self, config, repo):
        task_in_repo("no-tests", config, repo, UP_TO_BUILD)
        git(repo, "checkout", "-q", "-b", "relay/no-tests")
        git(repo, "rm", "-q", ".relay/profile.yaml")
        commit_all(repo)
        finish_step("no-tests", config, "ready-for-review")
        commands.cmd_next("no-tests", auto=False, config=config)
        assert current("no-tests", config) == ("in_progress", "builder")
        assert read_api.status_payload("no-tests", config)["test_runs"][-1]["outcome"] == "profile_broken"

    def test_a_failing_run_with_a_changed_command_just_goes_back(self, config, repo):
        task_on_branch("swap-red", config, repo, "exit 1")
        assert current("swap-red", config) == ("in_progress", "builder")
        assert read_api.status_payload("swap-red", config)["retries"]["builder"] == 1

    def test_sending_it_back_is_not_a_confirmation(self, config, repo):
        task_on_branch("swap-back", config, repo, "true")
        commands.cmd_send_back("swap-back", config, note="put the real tests back")
        finish_step("swap-back", config, "ready-for-review")
        commands.cmd_next("swap-back", auto=False, config=config)
        assert read_api.gate_check_payload("swap-back", config)["gate"]["state"] == "waiting_on_owner"


class TestDecideRoutingWithATestRun:
    CONFIG = {"human_gates": dict(settings.DEFAULT_CONFIG["human_gates"]), "max_retries": 3}

    def decide(self, phase, verdict, outcome, state=None):
        return routing.decide_routing(state or {"history": []}, phase, verdict, self.CONFIG,
                                    test_run={"outcome": outcome})

    @pytest.mark.parametrize("outcome", ["failed", "timed_out", "uncommitted", "profile_broken"])
    @pytest.mark.parametrize("phase, verdict", sorted(routing.VERDICTS_RELAY_TESTS))
    def test_a_run_that_did_not_pass_goes_back_to_the_builder(self, phase, verdict, outcome):
        decision = self.decide(phase, verdict, outcome)
        assert (decision.outcome, decision.next_phase, decision.counts_as_retry) == (
            "advance", "builder", True)

    @pytest.mark.parametrize("outcome", ["passed", "not_run"])
    def test_a_passing_or_absent_run_changes_nothing(self, outcome):
        decision = self.decide("builder", "ready-for-review", outcome)
        assert (decision.outcome, decision.next_phase, decision.counts_as_retry) == (
            "advance", "reviewer", False)

    def test_concerns_plus_a_failing_run_do_not_wait_for_the_owner(self):
        decision = self.decide("tester", "pass_with_concerns", "failed")
        assert (decision.outcome, decision.next_phase) == ("advance", "builder")

    def test_a_failing_run_at_the_retry_limit_stops_for_the_owner(self):
        decision = self.decide("builder", "ready-for-review", "failed",
                               state={"history": [], "retries": {"builder": 3}})
        assert (decision.outcome, decision.gate_kind) == ("retry_limit", "retry_limit")

    def test_a_changed_command_gates_until_the_owner_has_confirmed_it(self):
        changed = {"outcome": "passed", "command_differs_from_base": True}
        unconfirmed = routing.decide_routing({"history": []}, "builder", "ready-for-review",
                                           self.CONFIG, test_run=changed)
        assert (unconfirmed.outcome, unconfirmed.gate_kind, unconfirmed.next_phase) == (
            "owner_gate", "approval", "reviewer")
        confirmed = routing.decide_routing({"history": []}, "builder", "ready-for-review",
                                         self.CONFIG, test_run=changed, test_command_confirmed=True)
        assert (confirmed.outcome, confirmed.next_phase) == ("advance", "reviewer")

    def test_a_run_is_ignored_for_verdicts_that_do_not_claim_ready(self):
        decision = self.decide("reviewer", "changes_requested", "failed")
        assert (decision.next_phase, decision.reason) == (
            "builder", "reviewer verdict: changes_requested")


def test_the_ledger_event_is_a_test_run(config, repo):
    task_in_repo("event", config, repo, UP_TO_REVIEW)
    events = [event for event in db.get_events(settings.get_db_connection(), "event")
              if event["event_type"] == db.EVENT_TEST_RUN]
    assert [(event["phase"], event["step"]) for event in events] == [("builder", 3)]
    assert set(events[0]["payload"]) == RUN_KEYS
