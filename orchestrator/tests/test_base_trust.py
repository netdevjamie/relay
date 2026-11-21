"""Relay records the task's base as a commit it confirmed, and checks it
again before every launch.

`relay start` confirms the base: a remote-tracking base by asking the
remote itself, a local base from its exact branch, and refuses (recording
nothing) when it cannot. Before each later launch the commit is confirmed
again and moved forward only; a rewritten base stops the task, and an
unreachable remote leaves the recorded commit in use.

Every repo here is a throwaway under tmp_path; the remote is a local
folder, or a fake ssh command for a remote that hangs or prompts.
"""

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import agents
import arbiter_record
import base_trust
import commands
import db
import owner_notes
import read_api
import risk_record
import run_state
import settings
import sizing
from tests.git_fixtures import commit_all, git
from tests.test_phase_prompt import add_packed_ref
from tests.test_send_back import config  # noqa: F401  (a fixture)

TWIN_REFUSAL = ("Error: origin also has a branch MAIN, which this disk cannot keep apart from "
                "main; remove or rename it on origin, fetch, and start again.")


@dataclass
class Remote:
    upstream: Path
    clone: Path
    worktree: Path


@pytest.fixture
def remote(tmp_path, monkeypatch, config) -> Remote:
    """An upstream repo, a clone of it registered as `app`, and a worktree
    of the clone on `feature`, made from `origin/main`."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    (upstream / "README").write_text("app\n")
    commit_all(upstream, "the base")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    worktree = tmp_path / "feature"
    git(clone, "worktree", "add", "-q", "-b", "feature", str(worktree), "origin/main")
    settings.REGISTRY_PATH.write_text(f"repos:\n  app: {clone}\n")
    return Remote(upstream, clone, worktree)


@pytest.fixture
def launched(monkeypatch) -> list[str]:
    """The phase of every role Relay launches."""
    phases = []
    monkeypatch.setattr(agents, "invoke_agent",
                        lambda phase, *args, **kwargs: phases.append(phase) or 0)
    return phases


def start(remote: Remote, config: dict, task_id: str = "t", base: str | None = "origin/main") -> int:
    (settings.TASKS_DIR / task_id).mkdir(parents=True, exist_ok=True)
    (settings.TASKS_DIR / task_id / "brief.md").write_text("# Brief\n")
    return commands.cmd_start(task_id, auto=False, config=config, repo="app", base=base,
                              worktree=str(remote.worktree))


def base_events(task_id: str = "t") -> list[dict]:
    return [event["payload"] for event in db.get_events(settings.get_db_connection(), task_id)
            if event["event_type"] == db.EVENT_BASE_CONFIRMED]


def advance(upstream: Path, name: str = "next.txt") -> str:
    (upstream / name).write_text(f"{name}\n")
    return commit_all(upstream, f"add {name}")


def finish_the_current_step(task_id: str, verdict: str, config: dict) -> None:
    """Play the role whose step is latest: its report, with this verdict."""
    latest = read_api.status_payload(task_id, config)["latest_step"]
    (settings.TASKS_DIR / task_id / latest["artifact"]).write_text(
        f"---\ntask_id: {task_id}\nphase: {latest['phase']}\nstep: {latest['step']}\n"
        f"verdict: {verdict}\n---\n\nBody.\n")


def next_(capsys, config: dict, task_id: str = "t") -> tuple[int, str]:
    capsys.readouterr()
    code = commands.cmd_next(task_id, auto=False, config=config)
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# relay start
# ---------------------------------------------------------------------------

def test_start_records_the_remote_base_commit_and_prints_it(remote, config, launched, capsys):
    capsys.readouterr()
    assert start(remote, config) == 0
    commit = git(remote.upstream, "rev-parse", "main")
    assert [(event["commit"], event["ref"], event["how"], event["base"], event["from"])
            for event in base_events()] == [(commit, "refs/remotes/origin/main", "start", "origin/main", None)]
    assert run_state.load_run_state("t", config)["base"] == "origin/main"
    assert f"  Branch:   feature (base origin/main at {commit[:7]})\n" in capsys.readouterr().out


def test_start_records_the_commit_origin_names_when_the_local_copy_is_behind(remote, config, launched):
    older = git(remote.clone, "rev-parse", "refs/remotes/origin/main")
    newer = advance(remote.upstream)
    git(remote.clone, "fetch", "-q", "origin", newer)   # the commit, without moving the ref
    assert git(remote.clone, "rev-parse", "refs/remotes/origin/main") == older
    assert start(remote, config) == 0
    assert [event["commit"] for event in base_events()] == [newer]


def make_refusal(case: str, remote: Remote) -> tuple[str, str]:
    """Set up the case; return (the --base to give, the line Relay prints)."""
    if case == "unreachable":
        git(remote.clone, "remote", "set-url", "origin", str(remote.upstream.parent / "missing"))
        return "origin/main", ("Error: could not reach origin to confirm the base origin/main; "
                               "check the connection and start again.")
    if case == "no such branch":
        git(remote.clone, "update-ref", "refs/remotes/origin/gone", "HEAD")
        return "origin/gone", "Error: origin has no branch gone; check the name and start again."
    if case == "case twin on origin":
        add_packed_ref(remote.upstream, f"{git(remote.upstream, 'rev-parse', 'main')} refs/heads/MAIN".encode())
        return "origin/main", TWIN_REFUSAL
    if case == "local copy off origin":
        stray = git(remote.clone, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "stray")
        git(remote.clone, "update-ref", "refs/remotes/origin/main", stray)
        return "origin/main", (f"Error: origin/main here is {stray[:7]}, which is not on origin's main; "
                               f"fetch, and check no worktree was made from it.")
    if case == "origin ahead of this repo":
        newer = advance(remote.upstream)
        return "origin/main", (f"Error: origin's main is at {newer[:7]}, which this repo does not have "
                               f"yet; fetch and start again.")
    if case == "local case twin":
        add_packed_ref(remote.clone, f"{git(remote.clone, 'rev-parse', 'main')} refs/heads/MAIN".encode())
        return "main", ("Error: this repo has a ref refs/heads/MAIN, which this disk cannot keep apart "
                        "from refs/heads/main; remove it, and start again.")
    assert case == "a tag"
    git(remote.clone, "tag", "v1")
    return "v1", "Error: the base 'v1' is a tag, not a branch; give a branch, or a full commit id with --base."


@pytest.mark.parametrize("case", ["unreachable", "no such branch", "case twin on origin",
                                  "local copy off origin", "origin ahead of this repo",
                                  "local case twin", "a tag"])
def test_start_refuses_and_records_nothing(case, remote, config, launched, capsys):
    base, said = make_refusal(case, remote)
    capsys.readouterr()
    assert start(remote, config, base=base) == 1
    out = capsys.readouterr().out
    assert said in out
    assert "--worktree PATH" not in out
    assert run_state.load_run_state("t", config) is None
    assert db.get_events(settings.get_db_connection(), "t") == []
    assert launched == []


def test_a_local_base_is_read_from_its_exact_branch_without_asking_a_remote(remote, config, launched):
    git(remote.clone, "remote", "set-url", "origin", str(remote.upstream.parent / "missing"))
    stray = git(remote.clone, "commit-tree", "HEAD^{tree}", "-m", "a tag's commit")
    git(remote.clone, "tag", "main", stray)
    assert start(remote, config, base="main") == 0
    assert [(event["commit"], event["ref"]) for event in base_events()] == [
        (git(remote.clone, "rev-parse", "refs/heads/main"), "refs/heads/main")]


def a_fake_ssh(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-ssh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def gone(pid: int, within_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_a_remote_that_hangs_counts_as_unreachable_and_is_stopped(
        remote, config, launched, capsys, tmp_path, monkeypatch):
    pid_file = tmp_path / "ssh.pid"
    script = a_fake_ssh(tmp_path, f"echo $$ > '{pid_file}'\nexec sleep 600\n")
    git(remote.clone, "remote", "set-url", "origin", "ssh://example.invalid/r")
    git(remote.clone, "config", "core.sshCommand", str(script))
    monkeypatch.setattr(base_trust, "BASE_CHECK_SECONDS", 2)
    capsys.readouterr()
    started = time.monotonic()
    assert start(remote, config) == 1
    assert time.monotonic() - started < 10
    assert ("Error: could not reach origin to confirm the base origin/main; check the connection "
            "and start again.") in capsys.readouterr().out
    assert gone(int(pid_file.read_text()))


def test_a_remote_that_asks_for_credentials_gets_no_terminal_and_counts_as_unreachable(
        remote, config, launched, capsys, tmp_path):
    marker, env_file = tmp_path / "had-a-terminal", tmp_path / "ssh.env"
    script = a_fake_ssh(tmp_path, f"( exec 3</dev/tty ) 2>/dev/null && touch '{marker}'\n"
                                  f"env > '{env_file}'\nexit 255\n")
    git(remote.clone, "remote", "set-url", "origin", "ssh://example.invalid/r")
    git(remote.clone, "config", "core.sshCommand", str(script))
    capsys.readouterr()
    assert start(remote, config) == 1
    assert "Error: could not reach origin to confirm the base origin/main" in capsys.readouterr().out
    assert not marker.exists()
    seen = env_file.read_text().splitlines()
    assert {"GIT_TERMINAL_PROMPT=0", "SSH_ASKPASS_REQUIRE=never", "GCM_INTERACTIVE=never"} <= set(seen)


# ---------------------------------------------------------------------------
# Later steps
# ---------------------------------------------------------------------------

def test_next_moves_the_base_commit_forward(remote, config, launched, capsys):
    assert start(remote, config) == 0
    older = base_events()[-1]["commit"]
    newer = advance(remote.upstream)
    git(remote.clone, "fetch", "-q", "origin")
    finish_the_current_step("t", "ready-for-architect", config)
    code, out = next_(capsys, config)
    assert code == 0
    assert f"Base origin/main moved to {newer[:7]}.\n" in out
    assert (base_events()[-1]["commit"], base_events()[-1]["how"], base_events()[-1]["from"]) == (
        newer, "moved", older)
    assert launched == ["designer", "architect"]


def test_next_refuses_a_rewritten_base_and_launches_nothing(remote, config, launched, capsys):
    assert start(remote, config) == 0
    recorded = base_events()[-1]["commit"]
    git(remote.upstream, "commit", "-q", "--amend", "-m", "the base, rewritten")
    git(remote.clone, "fetch", "-q", "origin")
    finish_the_current_step("t", "ready-for-architect", config)
    events_before = db.get_events(settings.get_db_connection(), "t")
    code, out = next_(capsys, config)
    assert code == 1
    assert (f"Error: the base origin/main no longer contains {recorded[:7]}, the commit this task was "
            f"confirmed against; the base was rewritten, so check it before going on.") in out
    assert launched == ["designer"]
    assert db.get_events(settings.get_db_connection(), "t") == events_before
    assert db.get_task(settings.get_db_connection(), "t")["history"][-1].get("verdict") is None


def test_next_offline_runs_on_the_recorded_commit_and_says_so(remote, config, launched, capsys):
    assert start(remote, config) == 0
    recorded = base_events()[-1]["commit"]
    remote.upstream.rename(remote.upstream.parent / "upstream-away")
    finish_the_current_step("t", "ready-for-architect", config)
    code, out = next_(capsys, config)
    assert code == 0
    assert f"Base not re-checked (could not reach origin); using {recorded[:7]}.\n" in out
    assert launched == ["designer", "architect"]
    assert len(base_events()) == 1


def test_next_keeps_the_recorded_commit_when_origin_moved_but_was_not_fetched(
        remote, config, launched, capsys):
    assert start(remote, config) == 0
    recorded = base_events()[-1]["commit"]
    newer = advance(remote.upstream)
    finish_the_current_step("t", "ready-for-architect", config)
    code, out = next_(capsys, config)
    assert code == 0
    assert (f"Base not moved (origin's main is at {newer[:7]}, which this repo does not have yet; "
            f"fetch to pick it up); using {recorded[:7]}.\n") in out
    assert launched == ["designer", "architect"]
    assert [event["commit"] for event in base_events()] == [recorded]


def test_next_refuses_when_the_recorded_commit_is_not_in_the_repo(remote, config, launched, capsys):
    assert start(remote, config) == 0
    db.record_base_confirmed(settings.get_db_connection(), "t", base="origin/main",
                             ref="refs/remotes/origin/main", commit="1" * 40, previous=None, how="moved")
    finish_the_current_step("t", "ready-for-architect", config)
    code, out = next_(capsys, config)
    assert code == 1
    assert ("Error: the base commit 1111111 this task was confirmed against is not in this repo; "
            "fetch and run next again.") in out
    assert launched == ["designer"]


def a_task_from_before_base_commits(remote: Remote, config: dict, base: str) -> None:
    """A task as a Relay from before this change left it: its checkout and
    base recorded by name, and no base commit."""
    (settings.TASKS_DIR / "old").mkdir(parents=True)
    (settings.TASKS_DIR / "old" / "brief.md").write_text("# Brief\n")
    assert commands.cmd_start("old", auto=False, config=config) == 0
    state = run_state.load_run_state("old", config)
    state.update(repo="app", worktree_path=str(remote.worktree), branch="feature", base=base)
    run_state.save_run_state("old", state, config)
    assert base_events("old") == []


def test_a_task_recorded_before_this_change_gets_its_base_commit_at_next(remote, config, launched, capsys):
    a_task_from_before_base_commits(remote, config, "origin/main")
    finish_the_current_step("old", "ready-for-architect", config)
    code, _ = next_(capsys, config, "old")
    assert code == 0
    assert [(event["commit"], event["how"]) for event in base_events("old")] == [
        (git(remote.upstream, "rev-parse", "main"), "first")]


def test_a_task_recorded_before_this_change_on_a_tag_is_refused_at_next(remote, config, launched, capsys):
    git(remote.clone, "tag", "v1")
    a_task_from_before_base_commits(remote, config, "v1")
    finish_the_current_step("old", "ready-for-architect", config)
    code, out = next_(capsys, config, "old")
    assert code == 1
    assert "Error: the base 'v1' is a tag, not a branch; give a branch, or a full commit id with --base." in out
    assert launched == ["designer"]
    assert base_events("old") == []


def test_override_and_send_back_check_the_base_before_launching(remote, config, launched, capsys):
    config["human_gates"]["after_designer"] = True
    assert start(remote, config) == 0
    finish_the_current_step("t", "ready-for-architect", config)
    assert next_(capsys, config)[0] == 0
    assert read_api.gate_check_payload("t", config)["gate"]["state"] == "waiting_on_owner"
    add_packed_ref(remote.upstream, f"{git(remote.upstream, 'rev-parse', 'main')} refs/heads/MAIN".encode())
    capsys.readouterr()
    assert commands.cmd_send_back("t", config) == 1
    assert TWIN_REFUSAL in capsys.readouterr().out
    assert commands.cmd_override("t", "builder", config) == 1
    assert TWIN_REFUSAL in capsys.readouterr().out
    assert launched == ["designer"]
    assert commands.cmd_override("t", "done", config) == 0
    assert run_state.load_run_state("t", config)["status"] == "completed"


def test_an_older_relay_reads_a_ledger_with_base_confirmed_rows(remote, config, launched):
    """The rollback promise: no schema change, and every other reader of
    the ledger passes over base_confirmed rows."""
    assert start(remote, config) == 0
    connection = settings.get_db_connection()

    def what_the_readers_see():
        return (db.get_task(connection, "t"), db.generate_run_yaml(connection, "t"),
                db.get_retry_counts(connection, "t"), sizing.read_sizing("t", config),
                risk_record.read_risk_hints("t", config), owner_notes.read_owner_notes("t", config),
                arbiter_record.read_rulings("t", config), read_api.status_payload("t", config))

    before = what_the_readers_see()
    db.record_base_confirmed(connection, "t", base="origin/main", ref="refs/remotes/origin/main",
                             commit="2" * 40, previous=None, how="moved")
    assert what_the_readers_see() == before
    task = db.get_task(connection, "t")
    assert not {"base_commit", "base_ref"} & set(task)
    assert "base_commit" not in db.generate_run_yaml(connection, "t")


@pytest.mark.parametrize("payload", [
    {"base": "origin/main", "ref": "refs/remotes/origin/main", "commit": "not-a-commit"},
    {"base": "origin/main", "ref": "refs/remotes/origin/main", "commit": "A" * 40},
    {"base": "origin/main", "ref": 7, "commit": "1" * 40},
    {"base": "origin/main", "ref": "refs/remotes/origin/main", "commit": ["1" * 40]},
    {"base": "another-base", "ref": "refs/heads/another-base", "commit": "1" * 40},
    {"base": "origin/main"},
], ids=["not hex", "upper case", "ref not text", "commit not text", "another base's row", "no commit"])
def test_a_forged_or_broken_record_is_no_record_and_the_base_is_confirmed_again(
        payload, remote, config, launched, capsys):
    """The ledger row is read back, so a row of the wrong shape is never
    trusted: the base is confirmed again by the start rules."""
    assert start(remote, config) == 0
    db.record_event(settings.get_db_connection(), "t", db.EVENT_BASE_CONFIRMED, payload)
    finish_the_current_step("t", "ready-for-architect", config)
    code, _ = next_(capsys, config)
    assert code == 0
    assert (base_events()[-1]["commit"], base_events()[-1]["how"]) == (
        git(remote.upstream, "rev-parse", "main"), "first")


def what_is_recorded(task_id: str, config: dict) -> tuple:
    """Everything a step can write: the ledger's events and task row, and
    the run state."""
    connection = settings.get_db_connection()
    return (db.get_events(connection, task_id), db.get_task(connection, task_id),
            run_state.load_run_state(task_id, config))


@pytest.mark.parametrize("answer", ["send-back", "override", "override, accept and log"])
def test_a_refused_override_or_send_back_records_nothing(answer, remote, config, launched, capsys):
    """The base check comes before every write: a refusal leaves no
    approval, note, follow-up, verdict, step or send-back behind."""
    config["human_gates"]["after_designer"] = True
    assert start(remote, config) == 0
    finish_the_current_step("t", "ready-for-architect", config)
    assert next_(capsys, config)[0] == 0
    assert read_api.gate_check_payload("t", config)["gate"]["state"] == "waiting_on_owner"
    add_packed_ref(remote.upstream, f"{git(remote.upstream, 'rev-parse', 'main')} refs/heads/MAIN".encode())
    before = what_is_recorded("t", config)
    capsys.readouterr()
    if answer == "send-back":
        code = commands.cmd_send_back("t", config, note="the owner's words")
    elif answer == "override":
        code = commands.cmd_override("t", "builder", config, note="the owner's words")
    else:
        code = commands.cmd_override("t", "builder", config, note="the owner's words",
                                     log_items=[{"kind": "finding", "text": "a follow-up",
                                                "severity": "low", "from": "designer"}])
    assert code == 1
    assert TWIN_REFUSAL in capsys.readouterr().out
    assert what_is_recorded("t", config) == before
    assert launched == ["designer"]
