"""A branch that keeps the test command and changes what it runs is caught.

The files the base's test command names, common test runner configuration
and the base profile's `test_harness` list make up the harness. A passing
run on a branch that changed any of them stops for the owner, as a changed
command does, and the owner's acceptance covers that exact change.
"""

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import agents
import base_trust
import commands
import db
import evidence
import read_api
import settings
import test_harness
from tests.git_fixtures import commit_all, commit_of, git, write_profile
from tests.test_evidence import UP_TO_BUILD, current, task_in_repo
from tests.test_send_back import config, events_of_type, finish_step  # noqa: F401  (config is a fixture)


@pytest.fixture
def script_repo(tmp_path) -> Path:
    """A repo on `main` whose test command runs a script that fails."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "scripts").mkdir()
    (path / "scripts" / "run-tests.sh").write_text("echo the real tests; exit 1\n")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("x = 1\n")
    write_profile(path, "sh scripts/run-tests.sh")
    commit_all(path, "start")
    return path


def branch_edits(repo: Path, relative: str, content: str, branch: str | None = None) -> str:
    if branch:
        git(repo, "checkout", "-q", "-b", branch)
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return commit_all(repo)


def is_harness_at_fork(repo: Path, base_data=None, command="sh scripts/run-tests.sh"):
    fork = evidence.fork_point(repo, commit_of(repo))
    return test_harness.harness_rule(repo, fork, command, base_data)


class TestWhatCountsAsHarness:
    def test_runner_config_and_profile_listed_paths_are_harness_but_tests_and_ci_are_not(self, script_repo):
        (script_repo / "tests").mkdir()
        (script_repo / "tests" / "test_a.py").write_text("")
        commit_all(script_repo)
        is_harness = is_harness_at_fork(
            script_repo, {"test_harness": ["env/*.cfg", "seed.sql"]},
            command="cd tests && sh ../scripts/run-tests.sh tests/")
        for path in ["scripts/run-tests.sh", "conftest.py", "app/conftest.py", "pyproject.toml",
                     "web/jest.config.js", "env/test.cfg", "env/deep/test.cfg", "data/seed.sql"]:
            assert is_harness(path), path
        for path in ["tests/test_a.py", ".github/workflows/ci.yml", "tests", "src/app.py",
                     ".gitlab-ci.yml", "seed.sql.bak"]:
            assert not is_harness(path), path

    def test_tools_the_command_runs_bring_their_files(self, script_repo):
        is_harness = is_harness_at_fork(script_repo, command="npm test && make check")
        assert is_harness("package.json") and is_harness("Makefile")
        assert not is_harness_at_fork(script_repo, command="pytest -q")("package.json")

    def test_a_file_named_in_a_cd_folder_or_an_option_counts(self, script_repo):
        is_harness = is_harness_at_fork(script_repo,
                                        command="cd scripts && sh run-tests.sh --config=../ci/settings.ini")
        assert is_harness("scripts/run-tests.sh") and is_harness("ci/settings.ini")

    def test_every_configuration_file_pytest_reads_is_harness(self, script_repo):
        is_harness = is_harness_at_fork(script_repo, command="python3 -m pytest -q")
        for path in ["pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "pyproject.toml",
                     "tox.ini", "setup.cfg", "conftest.py", "sub/.pytest.ini", "sub/pytest.toml"]:
            assert is_harness(path), path

    def test_a_module_that_shadows_the_runner_where_the_command_runs_it_is_harness(self, script_repo):
        is_harness = is_harness_at_fork(script_repo, command="cd orchestrator && python3 -m pytest tests/")
        for path in ["orchestrator/pytest.py", "orchestrator/pytest/__init__.py", "pytest.py",
                     "pytest/main.py"]:
            assert is_harness(path), path
        for path in ["orchestrator/pytest_helpers.py", "lib/pytest.py", "orchestrator/tests/pytest.py"]:
            assert not is_harness(path), path
        assert not is_harness_at_fork(script_repo, command="pytest -q")("pytest.py")

    def test_harness_names_match_in_any_case(self, script_repo):
        """The Mac's disk ignores case: pytest looking for pytest.ini reads
        PYTEST.INI, and a command naming scripts/run-tests.sh runs
        SCRIPTS/RUN-TESTS.SH. So every rule compares names in any case."""
        is_harness = is_harness_at_fork(
            script_repo, {"test_harness": ["env/*.cfg", "seed.sql", "Fixtures/DB.cfg"]},
            command="npm test && npx x && yarn x && pnpm x && make check && sh scripts/run-tests.sh "
                    "&& python3 -m pytest && python3 -m MyRunner")
        runner_config = [pattern.replace("*", "js") for pattern in test_harness.DEFAULT_RUNNER_CONFIG]
        tool_files = [name for names in test_harness.TOOL_FILES.values() for name in names]
        for name in runner_config + tool_files:
            for variant in (name.upper(), name.swapcase()):
                assert is_harness(variant), variant
        for name in runner_config:
            assert is_harness("sub/" + name.upper()), name
        for path in ["SCRIPTS/RUN-TESTS.SH", "Scripts/Run-Tests.sh", "ENV/TEST.CFG", "data/SEED.SQL",
                     "fixtures/db.cfg", "PYTEST.PY", "Pytest/main.py", "myrunner.py", "MYRUNNER/main.py"]:
            assert is_harness(path), path
        for path in ["SRC/APP.PY", "TESTS/TEST_A.PY", "scripts/RUN-TESTS.SH.bak"]:
            assert not is_harness(path), path

    def test_test_files_are_found_by_folder_or_name(self):
        for path in ["tests/a.txt", "src/__tests__/a.js", "pkg/a_test.go", "web/a.spec.ts",
                     "src/test/java/com/a/BTest.java", "test_x.py"]:
            assert test_harness.is_test_file(path), path
        for path in ["src/app.py", "testing/helpers.py", "latest.py"]:
            assert not test_harness.is_test_file(path), path


class TestAChangedHarnessIsFlagged:
    def test_a_branch_that_edits_only_the_script_its_command_runs_stops_for_the_owner(
            self, config, script_repo, capsys):
        task_in_repo("harness", config, script_repo, UP_TO_BUILD)
        branch_edits(script_repo, "scripts/run-tests.sh", "exit 0\n", branch="relay/harness")
        finish_step("harness", config, "ready-for-review")
        assert commands.cmd_next("harness", auto=False, config=config) == 0
        out = capsys.readouterr().out
        run = read_api.status_payload("harness", config)["test_runs"][-1]
        assert (run["outcome"], run["command_differs_from_base"]) == ("passed", False)
        assert run["harness_changed"] == ["scripts/run-tests.sh"]
        gate = read_api.gate_check_payload("harness", config)["gate"]
        assert (gate["state"], gate["kind"], gate["next_phase"]) == ("waiting_on_owner", "approval", "reviewer")
        assert "changed test harness files (scripts/run-tests.sh)" in gate["reason"]
        assert "  This task changed how the repo is tested.\n" in out
        assert "  Harness files changed against the base: scripts/run-tests.sh\n" in out
        assert "Harness files changed against the base: scripts/run-tests.sh." in evidence.summary(run)
        assert current("harness", config) == ("in_progress", "builder")

    def test_the_harness_list_comes_from_the_base_not_the_branch(self, script_repo, tmp_path):
        write_profile(script_repo, "sh scripts/run-tests.sh", "test_harness:\n  - helpers/env.sh\n")
        (script_repo / "helpers").mkdir()
        (script_repo / "helpers" / "env.sh").write_text("export A=1\n")
        commit_all(script_repo)
        git(script_repo, "checkout", "-q", "-b", "relay/task")
        write_profile(script_repo, "sh scripts/run-tests.sh")
        git(script_repo, "mv", "helpers/env.sh", "helpers/renamed.sh")
        commit_all(script_repo)
        run = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=commit_of(script_repo))
        assert "helpers/env.sh" in run.harness_changed

    def test_branch_paths_with_pathspec_magic_are_taken_literally(self, script_repo, tmp_path):
        write_profile(script_repo, "sh scripts/run-tests.sh", "test_harness:\n  - ':(glob)*'\n")
        commit_all(script_repo)
        git(script_repo, "checkout", "-q", "-b", "relay/task")
        (script_repo / ":(glob)*").write_text("magic\n")
        commit_all(script_repo)
        first = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=commit_of(script_repo))
        again = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=commit_of(script_repo))
        assert first.harness_changed == [":(glob)*"]
        assert first.harness_digest == again.harness_digest is not None

    def test_a_change_off_the_harness_flags_nothing(self, script_repo, tmp_path):
        branch_edits(script_repo, "src/app.py", "x = 2\n", branch="relay/task")
        run = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=commit_of(script_repo))
        assert (run.harness_changed, run.harness_digest) == ([], None)
        assert evidence.needs_owner_confirmation(run.to_record()) is False


    def test_a_branch_that_adds_a_dot_pytest_ini_loading_its_own_plugin_stops_for_the_owner(self, tmp_path):
        """A plugin the branch loads from .pytest.ini
        marks a failing test passed, with the base's command and count."""
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / ".gitignore").write_text("reports/\n__pycache__/\n")
        write_profile(repo, f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
                            "--junitxml=reports/junit.xml tests", "test_report: reports/junit.xml\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n\n"
                                                  "def test_real():\n    assert 1 == 2\n")
        commit_all(repo, "start")
        git(repo, "checkout", "-q", "-b", "relay/task")
        (repo / ".pytest.ini").write_text("[pytest]\naddopts = -p quiet_plugin\n")
        (repo / "quiet_plugin.py").write_text(
            "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
            "def pytest_runtest_makereport(item, call):\n"
            "    out = yield\n    out.get_result().outcome = 'passed'\n")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt", base_commit=commit_of(repo))
        assert (run.outcome, run.counts["failures"], run.tests_drop) == ("passed", 0, None)
        assert evidence.needs_owner_confirmation(run.to_record()) is True
        assert run.harness_changed == [".pytest.ini"]

    def test_a_branch_that_adds_an_upper_case_pytest_ini_stops_for_the_owner(self, tmp_path):
        """On a Mac pytest reads PYTEST.INI as
        pytest.ini, so it loads the plugin as .pytest.ini did. On a disk
        that keeps case it would not, but the file is harness either way."""
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / ".gitignore").write_text("reports/\n__pycache__/\n")
        write_profile(repo, f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider "
                            "--junitxml=reports/junit.xml tests", "test_report: reports/junit.xml\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_a.py").write_text("def test_real():\n    assert 1 == 2\n")
        commit_all(repo, "start")
        git(repo, "checkout", "-q", "-b", "relay/task")
        (repo / "PYTEST.INI").write_text("[pytest]\naddopts = -p quiet_plugin\n")
        (repo / "quiet_plugin.py").write_text(
            "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
            "def pytest_runtest_makereport(item, call):\n"
            "    out = yield\n    out.get_result().outcome = 'passed'\n")
        commit_all(repo)
        run = evidence.run_tests(repo, tmp_path / "log.txt", base_commit=commit_of(repo))
        assert run.harness_changed == ["PYTEST.INI"]
        assert evidence.needs_owner_confirmation(run.to_record()) is True


class TestABranchWithNoSharedHistory:
    def test_an_orphan_branch_that_edits_the_script_stops_for_the_owner_and_says_why(
            self, config, script_repo, capsys):
        task_in_repo("orphan", config, script_repo, UP_TO_BUILD)
        git(script_repo, "checkout", "-q", "--orphan", "relay/orphan")
        (script_repo / "scripts" / "run-tests.sh").write_text("exit 0\n")
        commit_all(script_repo)
        finish_step("orphan", config, "ready-for-review")
        assert commands.cmd_next("orphan", auto=False, config=config) == 0
        gate = read_api.gate_check_payload("orphan", config)["gate"]
        assert (gate["state"], gate["next_phase"]) == ("waiting_on_owner", "reviewer")
        run = read_api.status_payload("orphan", config)["test_runs"][-1]
        assert (run["outcome"], run["command_differs_from_base"], run["harness_changed"]) == ("passed", False, [])
        assert run["harness_unchecked"] == evidence.HARNESS_UNCHECKED_NO_FORK
        assert f"Relay could not check the harness files ({evidence.HARNESS_UNCHECKED_NO_FORK})" in gate["reason"]
        assert f"  Harness files not checked: {evidence.HARNESS_UNCHECKED_NO_FORK}\n" in capsys.readouterr().out
        assert f"Harness files not checked: {evidence.HARNESS_UNCHECKED_NO_FORK}." in evidence.summary(run)

    def test_the_owners_acceptance_covers_that_commit_and_a_new_commit_asks_again(
            self, config, script_repo, capsys):
        task_in_repo("orphan-ok", config, script_repo, UP_TO_BUILD)
        git(script_repo, "checkout", "-q", "--orphan", "relay/orphan-ok")
        (script_repo / "scripts" / "run-tests.sh").write_text("exit 0\n")
        commit = commit_all(script_repo)
        finish_step("orphan-ok", config, "ready-for-review")
        assert commands.cmd_next("orphan-ok", auto=False, config=config) == 0
        assert read_api.gate_check_payload("orphan-ok", config)["gate"]["state"] == "waiting_on_owner"
        assert commands.cmd_override("orphan-ok", "reviewer", config) == 0
        assert "Accepted this task's unchecked harness at" in capsys.readouterr().out
        accepted = events_of_type("orphan-ok", db.EVENT_TEST_COMMAND_ACCEPTED)
        assert accepted[0]["payload"]["harness_unchecked_at"] == commit

        finish_step("orphan-ok", config, "approved")
        assert commands.cmd_next("orphan-ok", auto=False, config=config) == 0
        branch_edits(script_repo, "src/app.py", "x = 2\n")
        finish_step("orphan-ok", config, "pass")
        assert commands.cmd_next("orphan-ok", auto=False, config=config) == 0
        gate = read_api.gate_check_payload("orphan-ok", config)["gate"]
        assert gate["state"] == "waiting_on_owner"
        assert "Relay could not check the harness files" in gate["reason"]

    def test_a_diff_git_cannot_make_leaves_the_harness_unchecked(self, script_repo):
        run = evidence.TestRun(outcome="passed", commit=git(script_repo, "rev-parse", "HEAD"))
        missing_fork = evidence._BaseSetup(fork="0" * 40, rules=None, rules_unread=None,
                                           command="sh scripts/run-tests.sh", report=None)
        assert evidence._look_at_the_branch(run, script_repo, missing_fork) == []
        assert evidence.needs_owner_confirmation(run.to_record()) is True
        assert run.harness_unchecked == evidence.HARNESS_UNCHECKED_NO_DIFF


class TestTheOwnersAcceptance:
    def test_one_approval_covers_that_harness_change_and_a_further_change_asks_again(
            self, config, script_repo, capsys):
        task_in_repo("harness-ok", config, script_repo, UP_TO_BUILD)
        branch_edits(script_repo, "scripts/run-tests.sh", "exit 0\n", branch="relay/harness-ok")
        finish_step("harness-ok", config, "ready-for-review")
        assert commands.cmd_next("harness-ok", auto=False, config=config) == 0
        assert commands.cmd_override("harness-ok", "reviewer", config) == 0
        out = capsys.readouterr().out
        assert "Accepted this task's harness files as they are at" in out
        assert "Accepted this task's test command" not in out
        accepted = events_of_type("harness-ok", db.EVENT_TEST_COMMAND_ACCEPTED)
        assert accepted[0]["payload"]["harness_digest"]
        assert "tests_run" not in accepted[0]["payload"]

        finish_step("harness-ok", config, "approved")
        assert commands.cmd_next("harness-ok", auto=False, config=config) == 0
        branch_edits(script_repo, "src/app.py", "x = 2\n")
        finish_step("harness-ok", config, "pass")
        assert commands.cmd_next("harness-ok", auto=False, config=config) == 0
        assert current("harness-ok", config) == ("in_progress", "security")

    def test_a_further_harness_change_asks_again(self, config, script_repo):
        task_in_repo("harness-twice", config, script_repo, UP_TO_BUILD)
        branch_edits(script_repo, "scripts/run-tests.sh", "exit 0\n", branch="relay/harness-twice")
        finish_step("harness-twice", config, "ready-for-review")
        assert commands.cmd_next("harness-twice", auto=False, config=config) == 0
        assert commands.cmd_override("harness-twice", "reviewer", config) == 0
        finish_step("harness-twice", config, "approved")
        assert commands.cmd_next("harness-twice", auto=False, config=config) == 0
        branch_edits(script_repo, "scripts/run-tests.sh", "true\n")
        finish_step("harness-twice", config, "pass")
        assert commands.cmd_next("harness-twice", auto=False, config=config) == 0
        gate = read_api.gate_check_payload("harness-twice", config)["gate"]
        assert gate["state"] == "waiting_on_owner"
        assert "changed test harness files" in gate["reason"]

    def test_a_command_only_acceptance_is_stored_as_it_always_was(self, config, script_repo):
        task_in_repo("command-only", config, script_repo, UP_TO_BUILD)
        git(script_repo, "checkout", "-q", "-b", "relay/command-only")
        write_profile(script_repo, "true")
        commit_all(script_repo)
        finish_step("command-only", config, "ready-for-review")
        assert commands.cmd_next("command-only", auto=False, config=config) == 0
        reason = read_api.gate_check_payload("command-only", config)["gate"]["reason"]
        assert reason.endswith("; " + evidence.TEST_COMMAND_AWAITS_OWNER)
        assert commands.cmd_override("command-only", "reviewer", config) == 0
        accepted = events_of_type("command-only", db.EVENT_TEST_COMMAND_ACCEPTED)
        assert set(accepted[0]["payload"]) == {"command", "test_report", "commit"}


def base_with_rules_added_later(script_repo: Path, rules: str) -> str:
    """Make the base's tests pass only with an ignored marker file, commit
    that (the older commit, returned), then add `rules` to the base's
    profile in a newer commit on main."""
    (script_repo / ".gitignore").write_text("reports/\nmarker.ok\n")
    (script_repo / "scripts" / "run-tests.sh").write_text("test -f marker.ok\n")
    older = commit_all(script_repo, "the base before the owner's rules")
    write_profile(script_repo, "sh scripts/run-tests.sh", rules)
    commit_all(script_repo, "the owner adds rules to the base")
    return older


class TestTheRulesComeFromTheBasesTip:
    """`test_clean_checkout` and `test_harness` are read at the base's tip,
    not where the branch left its base, so a branch rebuilt on an older base
    commit cannot escape rules the owner added since."""

    RULES = "test_clean_checkout: true\ntest_harness:\n  - seed.sql\n"

    def test_a_branch_rebuilt_on_an_older_base_commit_still_gets_clean_mode_and_the_harness_list(
            self, script_repo, tmp_path):
        older = base_with_rules_added_later(script_repo, self.RULES)
        git(script_repo, "checkout", "-q", "-b", "relay/task", older)
        branch_edits(script_repo, "data/seed.sql", "insert 1;\n")
        (script_repo / "marker.ok").write_text("only in the worktree\n")
        assert evidence.fork_point(script_repo, commit_of(script_repo)) == older
        run = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=commit_of(script_repo))
        assert (run.clean_checkout, run.outcome) == (True, "failed")
        assert run.harness_changed == ["data/seed.sql"]

    def test_a_tag_named_like_the_base_does_not_choose_the_rules(self, script_repo, tmp_path):
        """A tag named like the base branch would make git pick the tag,
        which anyone who can write a ref can point at an older commit. The
        base commit Relay confirms is the branch's, so the rules the owner
        added to it apply."""
        older = base_with_rules_added_later(script_repo, self.RULES)
        git(script_repo, "tag", "main", older)
        git(script_repo, "checkout", "-q", "-b", "relay/task", older)
        branch_edits(script_repo, "data/seed.sql", "insert 1;\n")
        confirmed = base_trust.confirm_at_start(script_repo, base_trust.resolve(script_repo, "main"))
        assert confirmed == commit_of(script_repo) != older
        run = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=confirmed)
        assert (run.clean_checkout, run.harness_unchecked) == (True, None)
        assert run.harness_changed == ["data/seed.sql"]

    def test_a_branch_left_on_an_older_base_commit_keeps_the_rules_the_base_moved_to(
            self, config, script_repo, tmp_path, monkeypatch):
        """Through relay next: the recorded base
        commit moves forward to the one with the owner's rules, and a branch
        still on the older commit, whose suite passes only with an ignored
        file in its worktree, gets clean mode and the harness list."""
        (script_repo / ".gitignore").write_text("reports/\nmarker.ok\n")
        (script_repo / "scripts" / "run-tests.sh").write_text("test -f marker.ok\n")
        older = commit_all(script_repo, "the base before the owner's rules")
        clone = tmp_path / "clone"
        git(tmp_path, "clone", "-q", str(script_repo), str(clone))
        worktree = tmp_path / "feature"
        git(clone, "worktree", "add", "-q", "-b", "feature", str(worktree), "origin/main")
        settings.REGISTRY_PATH.write_text(f"repos:\n  app: {clone}\n")
        monkeypatch.setattr(agents, "invoke_agent", lambda *args, **kwargs: 0)
        (settings.TASKS_DIR / "rules").mkdir()
        (settings.TASKS_DIR / "rules" / "brief.md").write_text("# Brief\n")
        assert commands.cmd_start("rules", auto=False, config=config, repo="app", base="origin/main",
                                  worktree=str(worktree)) == 0
        write_profile(script_repo, "sh scripts/run-tests.sh", self.RULES)
        newer = commit_all(script_repo, "the owner adds rules to the base")
        git(clone, "fetch", "-q", "origin")
        for verdict, extra_frontmatter in UP_TO_BUILD:
            finish_step("rules", config, verdict, extra_frontmatter)
            assert commands.cmd_next("rules", auto=False, config=config) == 0
        assert events_of_type("rules", db.EVENT_BASE_CONFIRMED)[-1]["payload"]["commit"] == newer
        branch_edits(worktree, "data/seed.sql", "insert 1;\n")
        (worktree / "marker.ok").write_text("only in the worktree\n")
        assert evidence.fork_point(worktree, newer) == older
        finish_step("rules", config, "ready-for-review")
        assert commands.cmd_next("rules", auto=False, config=config) == 0
        run = read_api.status_payload("rules", config)["test_runs"][-1]
        assert (run["outcome"], run["clean_checkout"], run["harness_changed"]) == (
            "failed", True, ["data/seed.sql"])
        assert "Ran in a clean checkout" in evidence.summary(run)

    def test_a_base_tip_whose_profile_relay_cannot_read_leaves_the_harness_unchecked(
            self, script_repo, tmp_path):
        git(script_repo, "checkout", "-q", "-b", "relay/task")
        branch_edits(script_repo, "src/app.py", "x = 2\n")
        git(script_repo, "checkout", "-q", "main")
        (script_repo / ".relay" / "profile.yaml").write_bytes(
            b'schema_version: 1\ncommands:\n  test: "sh scripts/run-tests.sh"\n# \xff\n')
        commit_all(script_repo, "a base profile that is not UTF-8")
        git(script_repo, "checkout", "-q", "relay/task")
        run = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=commit_of(script_repo))
        assert run.harness_unchecked and "not UTF-8" in run.harness_unchecked


def submodule_repo(tmp_path: Path, name: str) -> Path:
    """A separate repo whose conftest.py could change any test's result."""
    path = tmp_path / name
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "conftest.py").write_text("# could patch pytest's reports\n")
    commit_all(path, "a helper")
    return path


def add_submodule(repo: Path, source: Path, where: str) -> str:
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), where)
    return commit_all(repo, "add a submodule")


class TestSubmodulesAreHarness:
    """The run loads files from
    inside a submodule that the branch's diff lists only as one path."""

    def test_a_branch_that_adds_a_submodule_has_changed_the_harness(self, script_repo, tmp_path):
        git(script_repo, "checkout", "-q", "-b", "relay/task")
        add_submodule(script_repo, submodule_repo(tmp_path, "helper"), "tests/aaa")
        run = evidence.run_tests(script_repo, tmp_path / "log.txt", base_commit=commit_of(script_repo))
        assert run.harness_changed == [".gitmodules", "tests/aaa"]

    def test_moving_a_submodule_to_another_commit_is_a_harness_change_and_not_a_test_change(
            self, script_repo, tmp_path):
        helper = submodule_repo(tmp_path, "helper")
        add_submodule(script_repo, helper, "tests/aaa")
        git(script_repo, "checkout", "-q", "-b", "relay/task")
        inside = script_repo / "tests" / "aaa"
        (inside / "conftest.py").write_text("# now it does\n")
        commit_all(inside, "change the helper")
        head = commit_all(script_repo, "move the submodule")
        fork = evidence.fork_point(script_repo, commit_of(script_repo))
        is_harness = test_harness.harness_rule(script_repo, fork, "sh scripts/run-tests.sh", None)
        paths, digest = test_harness.changed_harness(script_repo, fork, head, is_harness)
        assert (paths, digest is not None) == (["tests/aaa"], True)
        assert test_harness.changed_test_files(script_repo, fork, head, is_harness) == []

    def test_a_move_after_the_owner_accepted_an_ignore_all_gitmodules_changes_the_digest(
            self, script_repo, tmp_path):
        """The branch's own .gitmodules says
        `ignore = all`, which git's diff honours by default."""
        git(script_repo, "checkout", "-q", "-b", "relay/task")
        add_submodule(script_repo, submodule_repo(tmp_path, "helper"), "tests/aaa")
        ignore_all(script_repo, "tests/aaa")
        accepted = commit_all(script_repo, "ignore the submodule")
        fork = evidence.fork_point(script_repo, commit_of(script_repo))
        is_harness = test_harness.harness_rule(script_repo, fork, "sh scripts/run-tests.sh", None)
        _, accepted_digest = test_harness.changed_harness(script_repo, fork, accepted, is_harness)
        moved = move_submodule(script_repo, "tests/aaa")
        paths, moved_digest = test_harness.changed_harness(script_repo, fork, moved, is_harness)
        assert paths == [".gitmodules", "tests/aaa"]
        assert moved_digest != accepted_digest

    def test_a_base_gitmodules_with_ignore_all_does_not_hide_a_move(self, script_repo, tmp_path):
        """The Tester's step-14 case: the `ignore = all` is the base's."""
        add_submodule(script_repo, submodule_repo(tmp_path, "helper"), "tests/aaa")
        ignore_all(script_repo, "tests/aaa")
        commit_all(script_repo, "ignore the submodule")
        git(script_repo, "checkout", "-q", "-b", "relay/task")
        head = move_submodule(script_repo, "tests/aaa")
        fork = evidence.fork_point(script_repo, commit_of(script_repo))
        is_harness = test_harness.harness_rule(script_repo, fork, "sh scripts/run-tests.sh", None)
        paths, digest = test_harness.changed_harness(script_repo, fork, head, is_harness)
        assert (paths, digest is not None) == (["tests/aaa"], True)


def ignore_all(repo: Path, where: str) -> None:
    git(repo, "config", "-f", ".gitmodules", f"submodule.{where}.ignore", "all")


def move_submodule(repo: Path, where: str) -> str:
    """Commit a change inside the submodule, then point the repo at it."""
    inside = repo / where
    (inside / "conftest.py").write_text("# now it forces every test to pass\n")
    commit_all(inside, "change the helper")
    return commit_all(repo, "move the submodule")


class TestTheDocumentsMatchTheCode:
    DOCS = Path(__file__).resolve().parents[2] / "docs"

    def test_the_profile_document_lists_every_default_runner_config_name(self):
        profile_doc = (self.DOCS / "profile.md").read_text(encoding="utf-8")
        for name in test_harness.DEFAULT_RUNNER_CONFIG:
            assert f"`{name}`" in profile_doc, name

    def test_the_json_document_names_every_later_run_key(self):
        json_doc = (self.DOCS / "json-api.md").read_text(encoding="utf-8")
        first_keys = 12
        for key in list(evidence.TestRun.__dataclass_fields__)[first_keys:]:
            assert f"`{key}`" in json_doc, key
