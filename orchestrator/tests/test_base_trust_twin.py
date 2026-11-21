"""A base twin that differs only by case, for every reader of a task's
base.

On a case-insensitive disk (this Mac's default) the base has usually moved
since the clone, so a fetch has already written `refs/remotes/origin/main`
as a loose file. A later fetch of a remote branch `MAIN` writes the pushed
commit into that same file, which keeps its old name, so git itself then
reads `origin/main` as the pushed commit and lists no twin. No reader may
use that commit: `relay start` and `relay next` refuse while the twin is on
origin, and every reader works from the commit Relay recorded.

The same matrix runs with the twin packed (both refs in packed-refs) and
with tags named like the base; those run on any disk.

Every repo here is a throwaway under tmp_path, with a local folder as the
remote.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

import agents
import commands
import db
import recorded_runs
import risk_record
import run_state
import settings
from tests.git_fixtures import commit_all, git
from tests.test_phase_prompt import add_packed_ref, disk_ignores_case
from tests.test_send_back import config  # noqa: F401  (a fixture)

KINDS = ["loose", "packed", "tag"]
TWIN_KINDS = ["loose", "packed"]
TWIN_REFUSAL = ("Error: origin also has a branch MAIN, which this disk cannot keep apart from "
                "main; remove or rename it on origin, fetch, and start again.")

# The test command runs run.sh, which reads helpers/env.sh. Only the real
# base's profile lists that file as harness, so only a reader that uses the
# real base sees the branch's edit to it.
RUN_SH = """. ./helpers/env.sh
mkdir -p reports
if [ "$FAILURES" = 0 ]; then real='<testcase classname="t" name="real"/>'
else real='<testcase classname="t" name="real"><failure message="x"/></testcase>'; fi
printf '<testsuite tests="2" failures="%s" errors="0" skipped="0"><testcase classname="t" name="ok"/>%s</testsuite>' "$FAILURES" "$real" > reports/junit.xml
exit $CODE
"""
MAIN_PROFILE = """schema_version: 1
commands:
  test: "sh run.sh"
test_report: reports/junit.xml
test_harness:
  - helpers/env.sh
test_clean_checkout: true
risk_paths:
  auth: ["gatekeeper_*"]
"""
PUSHED_PROFILE = """schema_version: 1
commands:
  test: "sh run.sh"
test_report: reports/junit.xml
"""
BASE_FILES = {
    ".gitignore": "reports/\n",
    ".relay.yaml": "ops_addendum: OPS.md\n",
    "OPS.md": "MAIN-ADDENDUM\n",
    ".relay/profile.yaml": MAIN_PROFILE,
    ".relay/threat-model.md": "MAIN-THREAT-MODEL\n",
    "run.sh": RUN_SH,
    "helpers/env.sh": "FAILURES=1\nCODE=1\n",
}
PUSHED_FILES = {
    "OPS.md": "PUSHED-ADDENDUM\n",
    ".relay/profile.yaml": PUSHED_PROFILE,
    ".relay/threat-model.md": "PUSHED-THREAT-MODEL\n",
}


@dataclass
class World:
    upstream: Path
    clone: Path
    worktree: Path
    first: str      # the upstream's first commit, which the pushed commit is made off
    real: str       # the base's commit when the task starts


def write(folder: Path, files: dict) -> None:
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def world(tmp_path, monkeypatch, config) -> World:
    """An upstream with the real base, a clone registered as `app` whose
    `origin/main` is a loose ref (the base moved after the clone and was
    fetched), and a worktree of it on `feature` that edits the harness file
    and a path only the base's risk_paths name."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    write(upstream, BASE_FILES)
    first = commit_all(upstream, "the base")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    (upstream / "moved.txt").write_text("the base moves on\n")
    real = commit_all(upstream, "the base moves on")
    git(clone, "fetch", "-q", "origin")
    assert (clone / ".git" / "refs" / "remotes" / "origin" / "main").is_file()
    worktree = tmp_path / "feature"
    git(clone, "worktree", "add", "-q", "-b", "feature", str(worktree), "origin/main")
    write(worktree, {"helpers/env.sh": "FAILURES=0\nCODE=0\n", "src/gatekeeper_rules.py": "x = 1\n"})
    commit_all(worktree, "the task's work")
    registry = tmp_path / "registry.yaml"
    registry.write_text(f"repos:\n  app: {clone}\n")
    monkeypatch.setattr(settings, "REGISTRY_PATH", registry)
    return World(upstream, clone, worktree, first, real)


def pushed_commit(world: World) -> str:
    """A commit off the upstream's first commit carrying someone else's
    addendum, profile and threat model, on a branch that is then deleted."""
    git(world.upstream, "switch", "-q", "-c", "throwaway", world.first)
    write(world.upstream, PUSHED_FILES)
    pushed = commit_all(world.upstream, "pushed")
    git(world.upstream, "switch", "-q", "main")
    git(world.upstream, "branch", "-q", "-D", "throwaway")
    return pushed


def poison(world: World, kind: str) -> str:
    """Put the pushed commit where it could stand in for the base, as
    `kind` says, and return it."""
    if kind == "loose" and not disk_ignores_case(world.clone.parent):
        pytest.skip("this disk keeps MAIN and main apart, so a fetched MAIN cannot overwrite "
                    "the base's ref file")
    pushed = pushed_commit(world)
    if kind == "tag":
        git(world.upstream, "tag", "origin/main", pushed)
        git(world.upstream, "tag", "main", pushed)
        git(world.clone, "fetch", "-q", "--tags", "origin")
        return pushed
    add_packed_ref(world.upstream, f"{pushed} refs/heads/MAIN".encode())
    if kind == "loose":
        git(world.clone, "fetch", "-q", "origin")
        # The trick took: git itself now reads the base as the pushed commit.
        assert git(world.clone, "show-ref", "--verify", "--hash", "refs/remotes/origin/main") == pushed
    else:
        # As a fetch on a case-sensitive disk leaves it: two refs, both packed.
        git(world.clone, "fetch", "-q", "origin", "refs/heads/MAIN")
        add_packed_ref(world.clone, f"{pushed} refs/remotes/origin/MAIN".encode())
    return pushed


@pytest.fixture
def launched(monkeypatch) -> list[tuple[str, str]]:
    """(phase, prompt) of every role Relay launches."""
    seen = []
    monkeypatch.setattr(agents, "invoke_agent",
                        lambda phase, task_id, prompt, *args, **kwargs: seen.append((phase, prompt)) or 0)
    return seen


def start(world: World, config: dict, task_id: str = "t") -> int:
    (settings.TASKS_DIR / task_id).mkdir(parents=True, exist_ok=True)
    (settings.TASKS_DIR / task_id / "brief.md").write_text("# Brief\n")
    return commands.cmd_start(task_id, auto=False, config=config, repo="app", base="origin/main",
                              worktree=str(world.worktree))


def recorded_state(world: World, config: dict, task_id: str = "t") -> dict:
    """The task's state with the base commit Relay recorded at start, which
    is the commit the base was at then."""
    state = run_state.load_run_state(task_id, config)
    state["base_ref"], state["base_commit"] = "refs/remotes/origin/main", world.real
    return state


def finish_the_designer(task_id: str = "t") -> None:
    (settings.TASKS_DIR / task_id / "01-designer.md").write_text(
        f"---\ntask_id: {task_id}\nphase: designer\nstep: 1\nverdict: ready-for-architect\n---\n\nBody.\n")


# ---------------------------------------------------------------------------
# relay start and relay next refuse while the twin is on origin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", TWIN_KINDS)
def test_start_refuses_when_a_case_twin_is_on_origin(kind, world, config, launched, capsys):
    poison(world, kind)
    capsys.readouterr()
    assert start(world, config) == 1
    assert TWIN_REFUSAL in capsys.readouterr().out
    assert run_state.load_run_state("t", config) is None
    assert launched == []


@pytest.mark.parametrize("kind", TWIN_KINDS)
def test_next_refuses_when_a_case_twin_appears_after_start(kind, world, config, launched, capsys):
    assert start(world, config) == 0
    poison(world, kind)
    finish_the_designer()
    capsys.readouterr()
    assert commands.cmd_next("t", auto=False, config=config) == 1
    assert TWIN_REFUSAL in capsys.readouterr().out
    assert [phase for phase, _ in launched] == ["designer"]


# ---------------------------------------------------------------------------
# Each reader, from the recorded state, uses the recorded commit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_the_ops_prompt_from_the_recorded_state_leaves_out_the_pushed_addendum(
        kind, world, config, launched):
    assert start(world, config) == 0
    poison(world, kind)
    prompt = agents.build_prompt("ops", "t", 9, "09-ops.md", state=recorded_state(world, config))
    assert "MAIN-ADDENDUM" in prompt
    assert "PUSHED-ADDENDUM" not in prompt


@pytest.mark.parametrize("kind", KINDS)
def test_the_test_run_rules_and_fork_point_come_from_the_recorded_commit(
        kind, world, config, launched):
    assert start(world, config) == 0
    poison(world, kind)
    run = recorded_runs.ensure_test_run("t", recorded_state(world, config), "builder",
                                        "ready-for-review", 3, config)
    assert run["harness_changed"] == ["helpers/env.sh"]
    assert run["clean_checkout"] is True
    assert run["base_commit"] == world.real   # the fork point, from the recorded commit


@pytest.mark.parametrize("kind", KINDS)
def test_the_sizing_hint_reads_the_fork_and_risk_paths_at_the_recorded_commit(
        kind, world, config, launched):
    assert start(world, config) == 0
    poison(world, kind)
    hint = risk_record.ensure_risk_hint("t", recorded_state(world, config), 3, config)
    assert hint["fork"] == world.real
    auth = [found for found in hint["categories"] if found["category"] == "auth"]
    assert auth and "src/gatekeeper_rules.py" in auth[0]["paths"]


@pytest.mark.parametrize("phase", list(agents.AGENT_NAMES))
@pytest.mark.parametrize("kind", KINDS)
def test_the_role_prompt_names_the_recorded_commit(kind, phase, world, config, launched):
    assert start(world, config) == 0
    pushed = poison(world, kind)
    prompt = agents.build_prompt(phase, "t", 9, f"09-{phase}.md", state=recorded_state(world, config))
    assert f"(based on 'origin/main' at {world.real})" in prompt
    assert pushed not in prompt


def test_start_records_the_commit_the_base_was_at(world, config, launched):
    """What recorded_state stands for: the commit Relay records at start."""
    assert start(world, config) == 0
    recorded = [event["payload"] for event in db.get_events(settings.get_db_connection(), "t")
                if event["event_type"] == "base_confirmed"]
    assert [(payload["commit"], payload["ref"]) for payload in recorded] == [
        (world.real, "refs/remotes/origin/main")]
