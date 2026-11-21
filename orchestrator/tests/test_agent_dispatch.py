"""What Relay runs under `--auto`: `claude --agent <name>`, where the name
must be the `name:` line of the role's file in `agents/`, because that is
the only name Claude Code resolves. These tests read the real role files,
never a copy, and record the command instead of running `claude`.

A `claude` that exits non-zero stops Relay with code 1 and a plain line
that the role did not start or did not finish.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents
import awake
import commands
import settings
from tests.cli_edge import run_relay
from tests.test_send_back import UP_TO_REVIEW, config, finish_step, task_at  # noqa: F401

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
# The pipeline's roles, and the Arbiter, which Relay calls (settings.ARBITER).
DISPATCHABLE_PHASES = settings.PIPELINE_ORDER + [settings.ARBITER]
TASK = "dispatched"
NO_CONFIG: dict = {}


def role_file_name(phase: str) -> str:
    """The `name:` line of the frontmatter of agents/<phase>.md."""
    text = (AGENTS_DIR / f"{phase}.md").read_text()
    frontmatter = text.split("---")[1]
    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            return line.removeprefix("name:").strip()
    raise AssertionError(f"agents/{phase}.md has no name: line")


def record_commands(monkeypatch, returncode: int = 0) -> list:
    """Replace agents.run_role, the one place a role is started, with a
    recorder. Every call returns `returncode`; nothing is run."""
    ran = []

    def recorder(command, *args, **kwargs):
        ran.append(command)
        return agents.RoleRun(returncode=returncode, timed_out=False, ran_seconds=0)
    monkeypatch.setattr(agents, "run_role", recorder)
    return ran


def agent_name_in(command: list[str]) -> str:
    return command[command.index("--agent") + 1]


def claude_calls(ran: list) -> list:
    return [command for command in ran if isinstance(command, list) and command[0] == "claude"]


# ---------------------------------------------------------------------------
# The map against the real role files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", DISPATCHABLE_PHASES)
def test_every_dispatchable_phase_has_a_role_file_with_its_name(phase):
    assert (AGENTS_DIR / f"{phase}.md").is_file()
    assert role_file_name(phase) == agents.AGENT_NAMES[phase]


def test_the_map_names_exactly_the_dispatchable_phases():
    assert set(agents.AGENT_NAMES) == set(DISPATCHABLE_PHASES)


# ---------------------------------------------------------------------------
# The command Relay runs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", DISPATCHABLE_PHASES)
def test_auto_dispatch_passes_the_role_files_name(phase, monkeypatch):
    ran = record_commands(monkeypatch)
    agents.invoke_agent(phase, TASK, "the prompt", NO_CONFIG, auto=True)
    assert [agent_name_in(command) for command in claude_calls(ran)] == [role_file_name(phase)]


# ---------------------------------------------------------------------------
# When claude exits non-zero
# ---------------------------------------------------------------------------

def test_a_non_zero_exit_stops_relay_and_says_the_role_did_not_start(monkeypatch, capsys):
    record_commands(monkeypatch, returncode=1)
    assert agents.invoke_agent("tester", TASK, "the prompt", NO_CONFIG, auto=True) == 1
    out = capsys.readouterr().out
    assert "The Tester role did not start or did not finish: claude exited with code 1." in out
    assert "Relay stopped here" in out


def test_a_non_zero_exit_names_the_command_that_dispatches_the_phase_again(monkeypatch, capsys):
    record_commands(monkeypatch, returncode=2)
    assert agents.invoke_agent("reviewer", TASK, "the prompt", NO_CONFIG, auto=True) == 1
    assert f"{settings._CLI_CMD} override {TASK} reviewer --auto" in capsys.readouterr().out


def test_a_phase_with_no_role_file_is_refused_under_auto_before_claude_runs(
        monkeypatch, capsys, no_real_keep_awake):
    ran = record_commands(monkeypatch)
    assert agents.invoke_agent("pentest", TASK, "the prompt", NO_CONFIG, auto=True) == 1
    assert ran == []
    assert no_real_keep_awake == []
    assert "Nothing was started." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What stays as at main
# ---------------------------------------------------------------------------

def test_manual_mode_still_prints_the_phase(monkeypatch, capsys):
    ran = record_commands(monkeypatch)
    agents.invoke_agent("tester", TASK, "the prompt", NO_CONFIG, auto=False)
    assert ran == []
    assert "@tester the prompt" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# After a stop, the command it names dispatches the phase again
# ---------------------------------------------------------------------------

def test_override_dispatches_a_stopped_phase_again(config, monkeypatch):  # noqa: F811
    task_at(TASK, config, UP_TO_REVIEW[:2])                 # the Builder is running
    finish_step(TASK, config, "ready-for-review")
    ran = record_commands(monkeypatch, returncode=1)
    with pytest.raises(SystemExit):
        run_relay(config, "next", TASK, "--auto")   # the Reviewer did not start
    ran_again = record_commands(monkeypatch, returncode=0)
    assert commands.cmd_override(TASK, "reviewer", config, auto=True) == 0
    assert [agent_name_in(command) for command in claude_calls(ran + ran_again)] == [
        role_file_name("reviewer"), role_file_name("reviewer")]
