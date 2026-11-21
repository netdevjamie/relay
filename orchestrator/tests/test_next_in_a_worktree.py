"""`relay start` and `relay next` on a task whose worktree is a real git
checkout. `relay start` records the checkout it is given. Relay's own
test run reads the branch's and the base's profile, and a profile it cannot
read never ends `relay next` in an error. Also a whole walk to done.
"""

import subprocess
import time
from pathlib import Path

import pytest

import commands
import read_api
import run_state
import settings
from tests.cli_edge import run_relay
from tests.git_fixtures import build_in
from tests.hostile_yaml import BAD_VALUE_NAMES, BAD_VALUES, SECURITYS_MERGE_KEYS
from tests.test_send_back import config  # noqa: F401  (a fixture)
import project_profile

CLI = settings._CLI_CMD
TO_BUILDER = [("ready-for-architect", "Body.\n"), ("ready-for-builder", "Body.\n")]
REPO_PROFILE = 'schema_version: 1\ncommands:\n  test: "true"\n'
WALK = TO_BUILDER + [("ready-for-review", "Body.\n"), ("approved", "Body.\n"),
                     ("pass", "Body.\n"), ("secure", "Body.\n")]


def finish(task_id: str, config: dict, verdict: str, report: str, deploy: str = "no") -> None:
    """Play the agent: write the current step's artifact with this verdict
    and this report under Relay's frontmatter."""
    latest = read_api.status_payload(task_id, config)["latest_step"]
    extra = f"deploy: {deploy}\n" if verdict == "ready-for-builder" else ""
    (settings.TASKS_DIR / task_id / latest["artifact"]).write_text(
        f"---\ntask_id: {task_id}\nphase: {latest['phase']}\nstep: {latest['step']}\n"
        f"verdict: {verdict}\n{extra}---\n\n{report}")


def start(task_id: str, config: dict, worktree: Path | None = None) -> None:
    task_dir = settings.TASKS_DIR / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "brief.md").write_text("# Brief\n")
    assert commands.cmd_start(task_id, auto=False, config=config) == 0
    if worktree is not None:
        build_in(task_id, config, worktree)


def walk(task_id: str, config: dict, steps: list[tuple[str, str]], deploy: str = "no") -> None:
    for verdict, report in steps:
        finish(task_id, config, verdict, report, deploy)
        assert commands.cmd_next(task_id, auto=False, config=config) == 0


def builder_running(task_id: str, config: dict, worktree: Path) -> None:
    start(task_id, config, worktree)
    walk(task_id, config, TO_BUILDER)


def next_prints(task_id: str, config: dict, capsys) -> str:
    capsys.readouterr()
    assert commands.cmd_next(task_id, auto=False, config=config) == 0
    return capsys.readouterr().out


def where(task_id: str, config: dict) -> tuple[str, str]:
    task = read_api.status_payload(task_id, config)["task"]
    return task["status"], task["current_phase"]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def repo_with(tmp_path: Path, profile: str) -> Path:
    """A repo with a base on `main` and the task's branch checked out."""
    repo = tmp_path / "repo"
    (repo / ".relay").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".relay" / "profile.yaml").write_text(profile)
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "task")
    return repo


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def test_a_branch_profile_over_the_size_limit_stops_the_test_run(config, capsys, tmp_path):
    repo = repo_with(tmp_path, REPO_PROFILE)
    builder_running("larger", config, repo)
    (repo / ".relay" / "profile.yaml").write_text(
        REPO_PROFILE + "# " + "x" * project_profile.MAX_PROFILE_BYTES + "\n")
    commit_all(repo, "a profile over the limit")
    finish("larger", config, "ready-for-review", "Body.\n")
    out = next_prints("larger", config, capsys)
    assert "This branch has a project profile with errors, but its base has a test command" in out
    assert where("larger", config) == ("in_progress", "builder")


def test_a_branch_profile_holding_a_date_yaml_cannot_build_does_not_end_next_in_an_error(
        config, capsys, tmp_path):
    """At a Builder's `needs-architect`, where no test is run, this profile
    once ended `relay next` in a traceback and the task stayed with the
    Builder."""
    repo = repo_with(tmp_path, REPO_PROFILE)
    builder_running("bad-date", config, repo)
    (repo / ".relay" / "profile.yaml").write_text(REPO_PROFILE + "released: 2026-99-99\n")
    commit_all(repo, "a profile holding a date that does not exist")
    finish("bad-date", config, "needs-architect", "The plan has a gap.\n")
    next_prints("bad-date", config, capsys)
    assert where("bad-date", config) == ("in_progress", "architect")


@pytest.mark.parametrize("hostile", [SECURITYS_MERGE_KEYS, *BAD_VALUES],
                         ids=["Security's merge keys", *BAD_VALUE_NAMES])
def test_a_branch_profile_relay_does_not_read_stops_the_test_run_and_never_ends_next_in_an_error(
        config, capsys, tmp_path, hostile):
    """The branch's profile is not read: the test run refuses the branch as
    one whose profile is broken, and the task stays with the Builder."""
    repo = repo_with(tmp_path, REPO_PROFILE)
    builder_running("hostile", config, repo)
    (repo / ".relay" / "profile.yaml").write_text(REPO_PROFILE + hostile)
    commit_all(repo, "a profile Relay does not read")
    finish("hostile", config, "ready-for-review", "Body.\n")
    started = time.monotonic()
    out = next_prints("hostile", config, capsys)
    assert time.monotonic() - started < 10
    assert "This branch has a project profile with errors, but its base has a test command" in out
    assert where("hostile", config) == ("in_progress", "builder")


@pytest.mark.parametrize("hostile", [SECURITYS_MERGE_KEYS, *BAD_VALUES],
                         ids=["Security's merge keys", *BAD_VALUE_NAMES])
def test_a_base_profile_relay_does_not_read_flags_the_test_command_and_never_ends_next_in_an_error(
        config, capsys, tmp_path, hostile):
    """The profile at the task's base gives no base command, so the
    branch's is not the base's and the step stops for the owner."""
    repo = repo_with(tmp_path, REPO_PROFILE + hostile)
    builder_running("hostile-base", config, repo)
    (repo / ".relay" / "profile.yaml").write_text(REPO_PROFILE)
    commit_all(repo, "the branch's profile, which Relay reads")
    finish("hostile-base", config, "ready-for-review", "Body.\n")
    started = time.monotonic()
    out = next_prints("hostile-base", config, capsys)
    assert time.monotonic() - started < 10
    assert "The test command or report path is not the base branch's." in out
    assert where("hostile-base", config) == ("in_progress", "builder")


def test_sending_back_from_ops_names_it_as_a_sentence_does(config, capsys):
    start("to-ops", config)
    walk("to-ops", config, WALK, deploy="yes")
    assert where("to-ops", config) == ("in_progress", "ops")
    finish("to-ops", config, "not-ready", "The smoke test has no command.\n")
    out = next_prints("to-ops", config, capsys)
    assert f'    {CLI} send-back to-ops --note "your answer"   (back to Ops)\n' in out


def test_a_task_walks_to_done_through_every_phase(config):
    start("walked", config)
    walk("walked", config, WALK)
    assert where("walked", config) == ("completed", "done")
    history = read_api.status_payload("walked", config)["history"]
    assert [(entry["phase"], entry["verdict"]) for entry in history] == [
        ("designer", "ready-for-architect"), ("architect", "ready-for-builder"),
        ("builder", "ready-for-review"), ("reviewer", "approved"),
        ("tester", "pass"), ("security", "secure")]


# ---------------------------------------------------------------------------
# `relay start` records the checkout it is given and makes none
# ---------------------------------------------------------------------------

@pytest.fixture
def registered(tmp_path, monkeypatch):
    """A repo on `main` registered as `app`, and a worktree of it on the
    branch `feature`. Returns (repo, worktree)."""
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("app\n")
    commit_all(repo, "base")
    worktree = tmp_path / "app-feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    registry = tmp_path / "registry.yaml"
    registry.write_text(f"repos:\n  app: {repo}\n")
    monkeypatch.setattr(settings, "REGISTRY_PATH", registry)
    return repo, worktree


def brief(task_id: str) -> None:
    (settings.TASKS_DIR / task_id).mkdir(parents=True)
    (settings.TASKS_DIR / task_id / "brief.md").write_text("# Brief\n")


def started(task_id: str, config: dict, capsys, **given) -> tuple[int | None, str]:
    """(exit code or None, what was printed) for `relay start`, run through
    the command line with `given` as its --worktree and --base options."""
    brief(task_id)
    capsys.readouterr()
    options = [part for name, value in given.items() for part in (f"--{name}", value)]
    try:
        run_relay(config, "start", task_id, "--repo", "app", *options)
        code = None
    except SystemExit as stopped:
        code = stopped.code
    return code, capsys.readouterr().out


def test_start_records_the_worktree_its_branch_and_the_default_base(config, capsys, registered):
    repo, worktree = registered
    code, out = started("t", config, capsys, worktree=str(worktree))
    assert code is None
    state = run_state.load_run_state("t", config)
    assert (state["worktree_path"], state["branch"], state["base"]) == (
        str(worktree.resolve()), "feature", "main")
    assert f"  Worktree: {worktree.resolve()}\n" in out
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2   # none made


def test_start_takes_the_current_folder_inside_the_checkout(config, capsys, registered,
                                                            monkeypatch):
    _, worktree = registered
    (worktree / "src").mkdir()
    monkeypatch.chdir(worktree / "src")
    code, _ = started("here", config, capsys)
    assert code is None
    assert run_state.load_run_state("here", config)["worktree_path"] == str(worktree.resolve())


@pytest.mark.parametrize("where, said", [
    ("main", "is on 'main', the branch the task is based on"),
    ("detached", "has a detached HEAD"),
    ("other repo", "is not a checkout of"),
    ("not git", "is not a git checkout"),
])
def test_start_refuses_a_checkout_that_cannot_be_the_tasks(config, capsys, registered, tmp_path,
                                                            where, said):
    repo, worktree = registered
    if where == "main":
        checkout = repo
    elif where == "detached":
        git(worktree, "checkout", "-q", "--detach")
        checkout = worktree
    elif where == "other repo":
        checkout = tmp_path / "other"
        checkout.mkdir()
        git(checkout, "init", "-q", "-b", "feature")
        (checkout / "x").write_text("x\n")
        commit_all(checkout, "other")
    else:
        checkout = tmp_path / "plain"
        checkout.mkdir()
    code, out = started("refused", config, capsys, worktree=str(checkout))
    assert code == 1
    assert said in out and "--worktree PATH" in out
    assert run_state.load_run_state("refused", config) is None


def test_start_takes_a_base_that_exists_and_refuses_one_that_does_not(config, capsys, registered):
    repo, worktree = registered
    git(repo, "branch", "release")
    code, _ = started("based", config, capsys, worktree=str(worktree), base="release")
    assert code is None
    assert run_state.load_run_state("based", config)["base"] == "release"
    code, out = started("unbased", config, capsys, worktree=str(worktree), base="nope")
    assert code == 1 and "base branch 'nope' not found" in out
