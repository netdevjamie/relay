"""One way in: only base_trust.py hands a task's base name to git.

Every other reader takes the base commit base_trust confirmed. Two guards
hold that: a static one, which allows a read of the state's `base` only in
the functions listed below, none of which reaches git; and a recording
one, which runs every reader with a base whose name is unique and fails
when any module but base_trust puts that name in a git command.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import agents
import base_trust
import commands
import db
import recorded_runs
import risk_record
import run_state
import settings
from tests.git_fixtures import commit_all, git
from tests.test_send_back import config  # noqa: F401  (a fixture)

ORCHESTRATOR = Path(__file__).resolve().parent.parent

# (module, function) where `X["base"]` or `X.get("base")` may appear. Each
# one stores, copies or prints the name and never hands it to git. Anything
# in base_trust may: it is the one helper.
ALLOWED_BASE_READS = {
    ("db", "get_task"),                  # the ledger row into the state dict
    ("db", "generate_run_yaml"),         # the state dict into run.yaml text
    ("run_state", "save_run_state"),     # the state dict into the ledger
    ("run_state", "create_run"),         # the state dict into the ledger
    ("migrate_legacy", "import_task"),   # an old run.yaml into the ledger
    ("read_api", "status_payload"),      # shown in the JSON read API
    ("arbiter_record", "_step_lines"),   # shown in the Arbiter's handover
    ("agents", "_based_on"),             # shown in the role prompt, beside the commit
    ("commands", "cmd_start"),           # printed on the Branch line
}


def base_reads(module_path: Path) -> list[tuple[str | None, int]]:
    """(enclosing function, line) of every `X["base"]` and `X.get("base"…)`."""
    found = []

    def walk(node, function):
        for child in ast.iter_child_nodes(node):
            inside = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            subscript = (isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant)
                         and child.slice.value == "base")
            get_call = (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "get" and child.args
                        and isinstance(child.args[0], ast.Constant) and child.args[0].value == "base")
            if subscript or get_call:
                found.append((inside, child.lineno))
            walk(child, inside)

    walk(ast.parse(module_path.read_text()), None)
    return found


def test_only_base_trust_reads_the_task_base_name():
    outside = [f"{path.name}:{line} in {function}()"
               for path in sorted(ORCHESTRATOR.glob("*.py")) if path.stem != "base_trust"
               for function, line in base_reads(path)
               if (path.stem, function) not in ALLOWED_BASE_READS]
    assert outside == [], ("these read the task's base name outside base_trust; take "
                           "base_trust.trusted_commit(state) instead: " + ", ".join(outside))


def test_every_allowed_base_read_is_still_there():
    """A moved or renamed function fails here, so the list is looked at again."""
    present = {(path.stem, function) for path in ORCHESTRATOR.glob("*.py")
               for function, _ in base_reads(path)}
    assert ALLOWED_BASE_READS <= present, sorted(ALLOWED_BASE_READS - present)


# ---------------------------------------------------------------------------
# The recording guard
# ---------------------------------------------------------------------------

SENTINEL = "sentinel-base-7f3a"
PROFILE = """schema_version: 1
commands:
  test: "sh run.sh"
test_harness:
  - helpers/env.sh
test_clean_checkout: true
risk_paths:
  auth: ["gatekeeper_*"]
"""


def calling_module() -> str | None:
    """The orchestrator module (not a test) nearest the call, or None."""
    frame = sys._getframe(2)
    while frame is not None:
        path = Path(frame.f_code.co_filename).resolve()
        if path.parent == ORCHESTRATOR:
            return path.stem
        frame = frame.f_back
    return None


@pytest.fixture
def git_calls(monkeypatch) -> list[tuple[str | None, list[str]]]:
    """(calling module, argv) of every git command an orchestrator module runs."""
    calls = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def recorded(argv):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            calls.append((calling_module(), [str(part) for part in argv]))

    def run(argv, *args, **kwargs):
        recorded(argv)
        return real_run(argv, *args, **kwargs)

    class Popen(real_popen):
        def __init__(self, argv, *args, **kwargs):
            recorded(argv)
            super().__init__(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    return calls


@pytest.fixture
def sentinel_task(tmp_path, monkeypatch, config) -> dict:
    """A task started on a local base whose name is SENTINEL, in a
    worktree that edits the harness and a path only risk_paths names;
    returns its state with the confirmed base commit set, as every step
    has it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", SENTINEL)
    (repo / ".relay").mkdir()
    (repo / ".relay" / "profile.yaml").write_text(PROFILE)
    (repo / ".relay" / "threat-model.md").write_text("THREAT MODEL\n")
    (repo / ".relay.yaml").write_text("ops_addendum: OPS.md\n")
    (repo / "OPS.md").write_text("THE ADDENDUM\n")
    (repo / "run.sh").write_text(". ./helpers/env.sh\nexit $CODE\n")
    (repo / "helpers").mkdir()
    (repo / "helpers" / "env.sh").write_text("CODE=1\n")
    commit_all(repo, "the base")
    git(repo, "branch", "main")   # relay start wants a default branch too
    worktree = tmp_path / "feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "helpers" / "env.sh").write_text("CODE=0\n")
    (worktree / "src").mkdir()
    (worktree / "src" / "gatekeeper_rules.py").write_text("x = 1\n")
    commit_all(worktree, "the task's work")
    settings.REGISTRY_PATH.write_text(f"repos:\n  app: {repo}\n")
    monkeypatch.setattr(agents, "invoke_agent", lambda *args, **kwargs: 0)
    (settings.TASKS_DIR / "t").mkdir()
    (settings.TASKS_DIR / "t" / "brief.md").write_text("# Brief\n")
    assert commands.cmd_start("t", auto=False, config=config, repo="app", base=SENTINEL,
                              worktree=str(worktree)) == 0
    state = run_state.load_run_state("t", config)
    assert base_trust.check_before_step("t", state, config) is None
    assert base_trust.trusted_commit(state) == git(repo, "rev-parse", SENTINEL)
    return state


def run_every_reader(state: dict, config: dict) -> None:
    for phase in agents.AGENT_NAMES:
        agents.build_prompt(phase, "t", 3, f"03-{phase}.md", state=state, config=config)
    run = recorded_runs.ensure_test_run("t", state, "builder", "ready-for-review", 3, config)
    hint = risk_record.ensure_risk_hint("t", state, 3, config)
    # The readers did read the base (through its commit), so the guard below means something.
    assert run["harness_changed"] == ["helpers/env.sh"] and run["clean_checkout"] is True
    assert "src/gatekeeper_rules.py" in hint["categories"][0]["paths"]


def test_no_reader_hands_the_base_name_to_git(sentinel_task, config, git_calls):
    run_every_reader(sentinel_task, config)
    assert git_calls, "the readers ran no git command, so the guard saw nothing"
    by_name = [(module, argv) for module, argv in git_calls
               if module != "base_trust" and any(SENTINEL in part for part in argv)]
    assert by_name == [], f"these hand the base name to git outside base_trust: {by_name}"



# ---------------------------------------------------------------------------
# The recording guard, over relay start's own code
# ---------------------------------------------------------------------------

def a_sentinel_checkout(tmp_path: Path, kind: str) -> tuple[Path, Path, str]:
    """(the repo to register, the task's worktree, the --base to give) for
    a base named SENTINEL: a local branch, or that branch on origin."""
    first = tmp_path / ("upstream" if kind == "remote" else "repo")
    first.mkdir()
    git(first, "init", "-q", "-b", SENTINEL)
    (first / ".relay").mkdir()
    (first / ".relay" / "profile.yaml").write_text(PROFILE)
    (first / "README").write_text("app\n")
    commit_all(first, "the base")
    git(first, "branch", "main")   # relay start wants a default branch too
    repo, base = first, SENTINEL
    if kind == "remote":
        repo, base = tmp_path / "clone", f"origin/{SENTINEL}"
        git(tmp_path, "clone", "-q", str(first), str(repo))
    worktree = tmp_path / "feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree), base)
    return repo, worktree, base


@pytest.mark.parametrize("kind", ["local", "remote"])
def test_relay_start_hands_the_base_name_to_git_only_through_base_trust(
        kind, tmp_path, monkeypatch, config, git_calls):
    """relay start (cmd_start, task_checkout) reads the base name to print
    it and to hand it to base_trust; a lookup of its own would put the name
    in a git command from commands.py, and fails here."""
    repo, worktree, base = a_sentinel_checkout(tmp_path, kind)
    settings.REGISTRY_PATH.write_text(f"repos:\n  app: {repo}\n")
    monkeypatch.setattr(agents, "invoke_agent", lambda *args, **kwargs: 0)
    (settings.TASKS_DIR / "t").mkdir()
    (settings.TASKS_DIR / "t" / "brief.md").write_text("# Brief\n")
    git_calls.clear()   # only what start runs, not the set-up above
    assert commands.cmd_start("t", auto=False, config=config, repo="app", base=base,
                              worktree=str(worktree)) == 0
    named = [(module, argv) for module, argv in git_calls if any(SENTINEL in part for part in argv)]
    assert [module for module, _ in named if module == "base_trust"], (
        "start never confirmed the base through base_trust, so the guard saw nothing")
    by_name = [(module, argv) for module, argv in named if module != "base_trust"]
    assert by_name == [], f"relay start hands the base name to git outside base_trust: {by_name}"
    assert db.latest_base_confirmed(settings.get_db_connection(), "t")["commit"] == git(
        repo, "rev-parse", f"refs/{'remotes/' if kind == 'remote' else 'heads/'}{base}")
