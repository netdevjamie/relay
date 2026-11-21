"""Clean mode runs the tests where no ignored file can help them pass.

With `test_clean_checkout: true` in the base's or the branch's profile,
Relay runs the test command in a fresh checkout of the commit and deletes it
afterwards. With it off, the run notes the ignored files that were present.
"""

import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evidence
import scratch_checkout
from tests.git_fixtures import commit_all, commit_of, git, repo, write_profile  # noqa: F401  (repo is a fixture)

# Passes only when the ignored marker file is there.
NEEDS_MARKER = "test -f marker.ok"


def worktree_state(repo: Path) -> tuple[str, str]:
    return (git(repo, "status", "--porcelain"), git(repo, "status", "--porcelain", "--ignored"))


def scratch_folders() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob(f"{scratch_checkout.FOLDER_PREFIX}*"))


def with_marker(repo: Path, profile_extra: str) -> None:
    (repo / ".gitignore").write_text("reports/\nmarker.ok\n")
    write_profile(repo, NEEDS_MARKER, profile_extra)
    commit_all(repo)
    (repo / "marker.ok").write_text("only here\n")


def test_without_clean_mode_an_ignored_file_can_make_a_run_pass(repo, tmp_path):
    with_marker(repo, "")
    assert evidence.run_tests(repo, tmp_path / "log.txt").outcome == "passed"


def test_clean_mode_runs_in_a_fresh_checkout_and_leaves_the_worktree_untouched(repo, tmp_path):
    with_marker(repo, "test_clean_checkout: true\n")
    before, folders_before = worktree_state(repo), scratch_folders()
    run = evidence.run_tests(repo, tmp_path / "log.txt")
    assert (run.outcome, run.clean_checkout, run.ignored_present) == ("failed", True, None)
    assert worktree_state(repo) == before
    assert scratch_folders() == folders_before
    assert "Ran in a clean checkout." in evidence.summary(run.to_record())


def test_clean_mode_reads_the_report_from_the_fresh_checkout(repo, tmp_path):
    write_profile(repo, "mkdir -p reports && echo '<testsuite tests=\"2\" skipped=\"0\"/>' > reports/junit.xml",
                  "test_report: reports/junit.xml\ntest_clean_checkout: true\n")
    commit_all(repo)
    run = evidence.run_tests(repo, tmp_path / "log.txt")
    assert (run.outcome, run.tests_run) == ("passed", 2)
    assert not (repo / "reports").exists()


def test_the_branch_cannot_turn_clean_mode_off(repo, tmp_path):
    with_marker(repo, "test_clean_checkout: true\n")
    git(repo, "checkout", "-q", "-b", "relay/task")
    write_profile(repo, NEEDS_MARKER, "test_clean_checkout: false\n")
    commit_all(repo)
    run = evidence.run_tests(repo, tmp_path / "log.txt", base_commit=commit_of(repo))
    assert (run.outcome, run.clean_checkout) == ("failed", True)


def test_the_branch_can_turn_clean_mode_on(repo, tmp_path):
    with_marker(repo, "")
    git(repo, "checkout", "-q", "-b", "relay/task")
    write_profile(repo, NEEDS_MARKER, "test_clean_checkout: true\n")
    commit_all(repo)
    run = evidence.run_tests(repo, tmp_path / "log.txt", base_commit=commit_of(repo))
    assert (run.outcome, run.clean_checkout) == ("failed", True)


def test_ignored_files_present_are_noted_and_never_change_the_outcome(repo, tmp_path):
    (repo / ".gitignore").write_text("reports/\nnode_modules/\n.env\n")
    commit_all(repo)
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("")
    (repo / ".env").write_text("A=1\n")
    run = evidence.run_tests(repo, tmp_path / "log.txt")
    assert (run.outcome, run.ignored_present) == ("passed", [".env", "node_modules/"])
    assert "Ran with ignored files present: .env, node_modules/." in evidence.summary(run.to_record())


def test_a_run_with_no_ignored_files_says_nothing_of_them(repo, tmp_path):
    run = evidence.run_tests(repo, tmp_path / "log.txt")
    assert run.ignored_present == []
    assert "ignored" not in evidence.summary(run.to_record())


def test_a_clean_checkout_that_cannot_be_made_is_never_a_pass(repo, tmp_path, monkeypatch):
    write_profile(repo, "true", "test_clean_checkout: true\n")
    commit_all(repo)
    monkeypatch.setattr(scratch_checkout, "_git", lambda *args, **kwargs: _Failed())
    run = evidence.run_tests(repo, tmp_path / "log.txt")
    assert run.outcome == "failed"
    assert run.reason.startswith("Relay could not make the clean checkout the profile asks for.")


class _Failed:
    returncode = 128
    stdout = b""
    stderr = b"fatal: no\n"


def test_a_clean_run_that_writes_into_the_worktree_is_not_accepted(repo, tmp_path):
    stray = repo / "stray.txt"
    write_profile(repo, f"touch {shlex.quote(str(stray))}", "test_clean_checkout: true\n")
    commit_all(repo)
    run = evidence.run_tests(repo, tmp_path / "log.txt")
    assert run.outcome == "uncommitted"
    assert "stray.txt" in run.reason
