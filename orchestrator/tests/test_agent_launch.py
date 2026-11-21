"""What a role runs with under --auto, and the time limit it runs under.

Every role's model, effort, permission mode and time limit come from
Relay's own config (agents.launch_settings): a role's own value under
`roles:` wins, an unset one falls back to the global value, and then to
Claude's own default (model, effort) or Relay's (permission mode, limit).
These tests check the command Relay builds; no real `claude` runs, except
the stand-in script the time-limit test puts on PATH.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents
import settings

PROMPT = "the prompt"
TASK = "launched"


def command_for(phase: str, config: dict) -> list[str]:
    launch = agents.launch_settings(phase, config)
    return agents.claude_command(agents.AGENT_NAMES[phase], PROMPT, launch)


def flag_value(command: list[str], flag: str) -> str | None:
    return command[command.index(flag) + 1] if flag in command else None


@pytest.fixture
def no_task_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TASKS_DIR", tmp_path / "tasks")


# ---------------------------------------------------------------------------
# The time limit stops the role and everything it started
# ---------------------------------------------------------------------------

STAND_IN_CLAUDE = """#!/bin/bash
# A role that never finishes, with a child that ignores TERM, as a child
# that would outlive a plain stop of the role does.
( trap '' TERM; exec sleep 300 ) &
echo $! > "$PIDS_DIR/child"
echo $$ > "$PIDS_DIR/role"
sleep 300
"""


def _alarm(signal_number, frame):
    raise AssertionError("invoke_agent did not return: nothing stopped the role")


def _gone(pid: int, within_seconds: float) -> bool:
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _read_pid(path: Path, within_seconds: float) -> int:
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"the stand-in role never wrote {path.name}")


def _wait_for_mark(path: Path, within_seconds: float) -> None:
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"the stand-in role never wrote {path.name}")


def test_a_role_past_its_limit_is_stopped_with_every_process_it_started(
        tmp_path, monkeypatch, no_task_folder):
    stand_in = tmp_path / "bin" / "claude"
    stand_in.parent.mkdir()
    stand_in.write_text(STAND_IN_CLAUDE)
    stand_in.chmod(0o755)
    pids = tmp_path / "pids"
    pids.mkdir()
    monkeypatch.setenv("PATH", f"{stand_in.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PIDS_DIR", str(pids))
    monkeypatch.setattr(agents, "STOP_GRACE_SECONDS", 2)
    config = {"roles": {"tester": {"timeout_seconds": 1}}}

    # The 1-second limit runs from when Relay starts the role. On a loaded
    # machine the stand-in may need longer than that to write its pids, so
    # Relay is held until it has, and the limit then stops a running role.
    start_process = subprocess.Popen

    def start_then_wait_for_pids(command, *args, **kwargs):
        process = start_process(command, *args, **kwargs)
        if command[0] == "claude":
            _read_pid(pids / "role", 10)
            _read_pid(pids / "child", 10)
        return process

    monkeypatch.setattr(agents.subprocess, "Popen", start_then_wait_for_pids)

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(30)
    try:
        assert agents.invoke_agent("tester", TASK, PROMPT, config, auto=True) == 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    role, child = _read_pid(pids / "role", 5), _read_pid(pids / "child", 5)
    assert _gone(role, 5), "the role itself is still running"
    assert _gone(child, 5), "a process the role started is still running"


def test_an_interrupt_stops_the_role_group_and_is_raised_again(monkeypatch):
    stopped = []

    class Interrupted:
        pid = 4242

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(agents.subprocess, "Popen", Interrupted)
    monkeypatch.setattr(agents, "_stop_group", lambda role: stopped.append(role.pid))
    with pytest.raises(KeyboardInterrupt):
        agents.run_role(["claude"], None, 60)
    assert stopped == [4242]


# Relay, in a Python of its own, running the stand-in role with a long limit.
RELAY_RUNNING_A_ROLE = """
import sys
sys.path.insert(0, sys.argv[1])
import agents
agents.STOP_GRACE_SECONDS = 2
agents.run_role(["claude"], None, 300)
"""


@pytest.mark.parametrize("signal_number", [signal.SIGHUP, signal.SIGTERM],
                         ids=["closed terminal", "plain kill"])
def test_a_signal_that_ends_relay_stops_the_role_group_first(signal_number, tmp_path):
    stand_in = tmp_path / "bin" / "claude"
    stand_in.parent.mkdir()
    stand_in.write_text(STAND_IN_CLAUDE)
    stand_in.chmod(0o755)
    pids = tmp_path / "pids"
    pids.mkdir()
    environment = {**os.environ, "PATH": f"{stand_in.parent}{os.pathsep}{os.environ['PATH']}",
                   "PIDS_DIR": str(pids), "RELAY_HOME": str(tmp_path / "relay-home")}
    relay = subprocess.Popen(
        [sys.executable, "-c", RELAY_RUNNING_A_ROLE, str(Path(agents.__file__).parent)],
        env=environment)
    role = None
    try:
        role, child = _read_pid(pids / "role", 10), _read_pid(pids / "child", 10)
        relay.send_signal(signal_number)
        assert relay.wait(timeout=20) == -signal_number, "Relay did not end by the signal"
        assert _gone(role, 5), "the role itself is still running"
        assert _gone(child, 5), "a process the role started is still running"
    finally:
        if relay.poll() is None:
            relay.kill()
        if role is not None:
            agents._signal_group(role, signal.SIGKILL)  # leave nothing behind when it fails


# A role that ignores TERM, so a stop of it lasts the whole grace, and says
# when the TERM came: the grace has begun.
STAND_IN_IGNORING_TERM = """#!/bin/bash
trap 'echo > "$PIDS_DIR/termed"' TERM
( trap '' TERM; exec sleep 300 ) &
echo $! > "$PIDS_DIR/child"
echo $$ > "$PIDS_DIR/role"
while true; do sleep 0.2; done
"""

# Relay, in a Python of its own, holding the role's start until the role
# has written its pid, so the limit (argv 3) runs out on a running role.
# It marks when it goes on to wait for the role, and writes how the run
# ended to a file: its output is the role's too, which outlives it on a
# failure.
RELAY_STOPPING_A_ROLE = """
import pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import agents
agents.STOP_GRACE_SECONDS = 4
pids = pathlib.Path(sys.argv[2])
start_process = subprocess.Popen
def start_then_wait_for_the_role(command, *args, **kwargs):
    process = start_process(command, *args, **kwargs)
    while not (pids / "role").exists():
        time.sleep(0.05)
    (pids / "relay-waiting").write_text("")
    return process
agents.subprocess.Popen = start_then_wait_for_the_role
run = agents.run_role(["claude"], None, int(sys.argv[3]))
(pids / "ended").write_text("timed out" if run.timed_out else "finished")
"""


@pytest.mark.parametrize("first, second, ended_by", [
    (None, signal.SIGHUP, 0),
    (None, signal.SIGTERM, 0),
    (None, signal.SIGINT, 0),
    (signal.SIGINT, signal.SIGHUP, -signal.SIGINT),
    (signal.SIGHUP, signal.SIGINT, -signal.SIGHUP),
], ids=["time limit, then a closed terminal", "time limit, then a plain kill",
        "time limit, then Ctrl-C", "Ctrl-C, then a closed terminal",
        "closed terminal, then Ctrl-C"])
def test_a_second_signal_during_the_grace_does_not_cut_the_stop_short(first, second, ended_by,
                                                                     tmp_path):
    stand_in = tmp_path / "bin" / "claude"
    stand_in.parent.mkdir()
    stand_in.write_text(STAND_IN_IGNORING_TERM)
    stand_in.chmod(0o755)
    pids = tmp_path / "pids"
    pids.mkdir()
    environment = {**os.environ, "PATH": f"{stand_in.parent}{os.pathsep}{os.environ['PATH']}",
                   "PIDS_DIR": str(pids), "RELAY_HOME": str(tmp_path / "relay-home")}
    limit = 1 if first is None else 300
    relay = subprocess.Popen(
        [sys.executable, "-c", RELAY_STOPPING_A_ROLE, str(Path(agents.__file__).parent),
         str(pids), str(limit)],
        env=environment)
    role = None
    try:
        role, child = _read_pid(pids / "role", 10), _read_pid(pids / "child", 10)
        if first is not None:
            _wait_for_mark(pids / "relay-waiting", 10)
            time.sleep(0.5)  # the few lines from the mark to Relay's wait
            relay.send_signal(first)
        _wait_for_mark(pids / "termed", 10)  # the stop has begun its grace
        relay.send_signal(second)
        ended = relay.wait(timeout=20)
        assert _gone(role, 5), "the role itself is still running"
        assert _gone(child, 5), "a process the role started is still running"
        assert ended == ended_by, "Relay did not end the way the first cause says"
        if first is None:
            assert (pids / "ended").read_text() == "timed out"
    finally:
        if relay.poll() is None:
            relay.kill()
        if role is not None:
            agents._signal_group(role, signal.SIGKILL)  # leave nothing behind when it fails


def test_a_signal_relay_ignores_stays_ignored_while_the_role_runs(monkeypatch):
    seen = []

    class Finishing:
        pid = 4242

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout=None):
            seen.append((signal.getsignal(signal.SIGHUP), signal.getsignal(signal.SIGTERM)))
            return 0

    previous_hangup = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        monkeypatch.setattr(agents.subprocess, "Popen", Finishing)
        assert agents.run_role(["claude"], None, 60).returncode == 0
        assert seen[0][0] == signal.SIG_IGN, "an ignored SIGHUP was caught while the role ran"
        assert seen[0][1] not in (signal.SIG_DFL, signal.SIG_IGN), "SIGTERM was not caught"
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL, "SIGTERM was not given back"
    finally:
        signal.signal(signal.SIGHUP, previous_hangup)


def test_relay_gets_its_own_handlers_back_after_a_time_limit_stop(monkeypatch):
    # A stop ignores Ctrl-C, SIGHUP and SIGTERM through its grace. Once it
    # ends, Relay's own handlers must be back: else a later Ctrl-C is lost,
    # and every process Relay starts afterwards inherits the ignored signal.
    during_stop = []

    class PastItsLimit:
        pid = 4242

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("claude", timeout)

    def stop(role):
        during_stop.append({number: signal.getsignal(number)
                            for number in agents.STOP_IGNORES_SIGNALS})

    before = {number: signal.getsignal(number) for number in agents.STOP_IGNORES_SIGNALS}
    monkeypatch.setattr(agents.subprocess, "Popen", PastItsLimit)
    monkeypatch.setattr(agents, "_stop_group", stop)
    try:
        assert agents.run_role(["claude"], None, 1).timed_out
        assert during_stop == [{number: signal.SIG_IGN for number in before}]
        after = {number: signal.getsignal(number) for number in agents.STOP_IGNORES_SIGNALS}
        assert after == before, "a handler Relay had was not given back after the stop"
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)


def test_a_role_that_finishes_in_time_gives_its_exit_code(tmp_path):
    finished = agents.run_role(["/bin/sh", "-c", "exit 3"], str(tmp_path), 30)
    assert (finished.returncode, finished.timed_out) == (3, False)


# ---------------------------------------------------------------------------
# The command Relay builds
# ---------------------------------------------------------------------------

def test_the_command_with_nothing_new_set_is_todays_plus_the_permission_mode():
    assert command_for("builder", {}) == [
        "claude", "--agent", "Builder", "--print", PROMPT,
        "--permission-mode", "bypassPermissions"]
    assert command_for("builder", {"model": "opus"}) == [
        "claude", "--agent", "Builder", "--print", PROMPT, "--model", "opus",
        "--permission-mode", "bypassPermissions"]


@pytest.mark.parametrize("config, expected", [
    ({"model": "global-model", "roles": {"reviewer": {"model": "role-model"}}}, "role-model"),
    ({"model": "global-model"}, "global-model"),
    ({}, None),
], ids=["role value", "global value only", "neither"])
def test_model_falls_back_role_then_global_then_none(config, expected):
    assert flag_value(command_for("reviewer", config), "--model") == expected


@pytest.mark.parametrize("config, expected", [
    ({"effort": "high", "roles": {"reviewer": {"effort": "max"}}}, "max"),
    ({"effort": "high"}, "high"),
    ({}, None),
], ids=["role value", "global value only", "neither"])
def test_effort_falls_back_role_then_global_then_none(config, expected):
    assert flag_value(command_for("reviewer", config), "--effort") == expected


def test_a_role_value_reaches_that_role_only():
    config = {"roles": {"tester": {"model": "role-model", "effort": "low"}}}
    assert flag_value(command_for("builder", config), "--model") is None
    assert flag_value(command_for("builder", config), "--effort") is None


@pytest.mark.parametrize("phase", list(agents.AGENT_NAMES))
def test_every_role_gets_its_default_permission_mode(phase):
    assert flag_value(command_for(phase, {}), "--permission-mode") == "bypassPermissions"
    assert command_for(phase, {}).count("--permission-mode") == 1


def test_a_configured_mode_reaches_that_role_only():
    config = {"roles": {"ops": {"permission_mode": "dontAsk"}}}
    modes = {phase: flag_value(command_for(phase, config), "--permission-mode")
             for phase in agents.AGENT_NAMES}
    assert modes.pop("ops") == "dontAsk"
    assert set(modes.values()) == {"bypassPermissions"}


@pytest.mark.parametrize("bad_mode", ["yolo", "", 3, ["bypassPermissions"]])
def test_a_bad_mode_warns_and_uses_the_default(bad_mode):
    launch = agents.launch_settings("ops", {"roles": {"ops": {"permission_mode": bad_mode}}})
    assert launch.permission_mode == "bypassPermissions"
    assert launch.warnings == (
        f"Warning: roles.ops.permission_mode must be one of "
        f"{', '.join(agents.PERMISSION_MODES)} (found {bad_mode!r}). Using bypassPermissions.",)


@pytest.mark.parametrize("bad_effort", ["extreme", 5, True])
def test_a_bad_effort_warns_and_falls_back_to_the_global(bad_effort):
    launch = agents.launch_settings(
        "tester", {"effort": "medium", "roles": {"tester": {"effort": bad_effort}}})
    assert launch.effort == "medium"
    assert len(launch.warnings) == 1
    assert "roles.tester.effort" in launch.warnings[0] and repr(bad_effort) in launch.warnings[0]


@pytest.mark.parametrize("bad_model", ["", "   ", 7, ["opus"]])
def test_a_bad_model_warns_and_falls_back_to_the_global(bad_model):
    launch = agents.launch_settings(
        "tester", {"model": "global-model", "roles": {"tester": {"model": bad_model}}})
    assert launch.model == "global-model"
    assert len(launch.warnings) == 1 and "roles.tester.model" in launch.warnings[0]


# ---------------------------------------------------------------------------
# The time limit's config
# ---------------------------------------------------------------------------

def test_the_default_limit_is_two_hours_for_every_role():
    assert settings.DEFAULT_CONFIG["agent_timeout_seconds"] == 2 * 60 * 60
    for phase in agents.AGENT_NAMES:
        assert agents.launch_settings(phase, dict(settings.DEFAULT_CONFIG)).timeout_seconds == 7200
        assert agents.launch_settings(phase, {}).timeout_seconds == 7200


def test_a_role_limit_wins_over_the_global_limit():
    config = {"agent_timeout_seconds": 600, "roles": {"builder": {"timeout_seconds": 900}}}
    assert agents.launch_settings("builder", config).timeout_seconds == 900
    assert agents.launch_settings("tester", config).timeout_seconds == 600


BAD_TIMEOUTS = [0, -5, "2h", True, 1.5, [60]]


@pytest.mark.parametrize("bad", BAD_TIMEOUTS, ids=repr)
def test_a_bad_role_timeout_warns_naming_the_role_and_the_value(bad):
    config = {"agent_timeout_seconds": 600, "roles": {"tester": {"timeout_seconds": bad}}}
    launch = agents.launch_settings("tester", config)
    assert launch.timeout_seconds == 600
    assert launch.warnings == (
        f"Warning: roles.tester.timeout_seconds must be a whole number of seconds above 0 "
        f"(found {bad!r}). Using 600.",)


@pytest.mark.parametrize("bad", BAD_TIMEOUTS, ids=repr)
def test_a_bad_global_timeout_warns_naming_the_role_and_the_value(bad):
    launch = agents.launch_settings("reviewer", {"agent_timeout_seconds": bad})
    assert launch.timeout_seconds == 7200
    assert launch.warnings == (
        f"Warning: agent_timeout_seconds (for reviewer) must be a whole number of seconds "
        f"above 0 (found {bad!r}). Using 7200.",)


# ---------------------------------------------------------------------------
# Config Relay does not know
# ---------------------------------------------------------------------------

def test_an_unknown_role_name_warns_and_is_not_applied():
    # The removed process auditor: a role name Relay no longer knows.
    config = {"roles": {"auditor": {"model": "elsewhere"}, "tester": {"model": "role-model"}}}
    launch = agents.launch_settings("builder", config)
    assert launch.model is None
    assert launch.warnings == (
        f"Warning: roles.auditor is not one of Relay's roles "
        f"({', '.join(agents.AGENT_NAMES)}); ignored.",)


def test_an_unknown_setting_warns_and_the_known_ones_still_apply():
    launch = agents.launch_settings("tester", {"roles": {"tester": {"modle": "x", "effort": "low"}}})
    assert launch.effort == "low"
    assert len(launch.warnings) == 1 and "roles.tester.modle" in launch.warnings[0]


@pytest.mark.parametrize("roles", ["builder: opus", ["builder"], 3])
def test_roles_that_are_not_a_mapping_warn_and_are_ignored(roles):
    launch = agents.launch_settings("builder", {"roles": roles})
    assert (launch.model, launch.permission_mode, launch.timeout_seconds) == (
        None, "bypassPermissions", 7200)
    assert len(launch.warnings) == 1 and launch.warnings[0].startswith("Warning: roles must be")


@pytest.mark.parametrize("own", ["opus", ["opus"], 3])
def test_a_role_entry_that_is_not_a_mapping_warns_and_is_ignored(own):
    launch = agents.launch_settings("builder", {"model": "global-model", "roles": {"builder": own}})
    assert launch.model == "global-model"
    assert len(launch.warnings) == 1 and "roles.builder must be" in launch.warnings[0]


# ---------------------------------------------------------------------------
# What invoke_agent prints
# ---------------------------------------------------------------------------

def test_the_launch_line_names_model_effort_permission_and_limit(monkeypatch, capsys,
                                                                   no_task_folder):
    monkeypatch.setattr(agents, "run_role", lambda *args: agents.RoleRun(0, False, 1))
    assert agents.invoke_agent("builder", TASK, PROMPT, {}, auto=True) == 0
    assert ("Invoking @builder automatically (model: default, effort: default, "
            "permission: bypassPermissions, limit 120 min)...") in capsys.readouterr().out

    config = {"model": "opus", "roles": {"builder": {"effort": "max", "timeout_seconds": 90}}}
    assert agents.invoke_agent("builder", TASK, PROMPT, config, auto=True) == 0
    assert ("Invoking @builder automatically (model: opus, effort: max, "
            "permission: bypassPermissions, limit 90 s)...") in capsys.readouterr().out


def test_a_bad_value_is_printed_as_a_warning_and_the_role_still_starts(monkeypatch, capsys,
                                                                         no_task_folder):
    started = []
    monkeypatch.setattr(agents, "run_role",
                        lambda command, cwd, timeout: started.append(timeout) or agents.RoleRun(0, False, 1))
    assert agents.invoke_agent("builder", TASK, PROMPT, {"agent_timeout_seconds": "2h"},
                               auto=True) == 0
    assert started == [7200]
    assert "  Warning: agent_timeout_seconds (for builder)" in capsys.readouterr().out


MANUAL_OUTPUT_AT_MAIN = (
    "\n"
    "  Run this in Claude Code:\n"
    "\n"
    "    @tester the prompt\n"
    "\n"
    "  Working directory: /some/worktree\n"
    "  Then run: relay next launched\n")


def test_manual_mode_prints_exactly_as_before(monkeypatch, capsys, no_task_folder):
    monkeypatch.setattr(agents, "run_role", lambda *args: pytest.fail("manual mode ran a role"))
    config = {"roles": {"tester": {"timeout_seconds": "bad", "permission_mode": "bad"}}}
    assert agents.invoke_agent("tester", TASK, PROMPT, config, auto=False, cwd="/some/worktree") == 0
    assert capsys.readouterr().out == MANUAL_OUTPUT_AT_MAIN
