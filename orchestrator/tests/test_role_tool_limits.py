"""Every read-only role is held to read-only
tools by Claude Code itself, not only by the words of its role file. Each
such role lacks Edit, and a PreToolUse hook lets its Write reach only its
own report under Relay's tasks folder, or a temporary folder."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "hooks" / "only-write-reports.sh"
READ_ONLY_ROLES = ("designer", "architect", "reviewer", "security", "ops")
HOOK_COMMAND = '"${RELAY_HOME:-$HOME/Projects/.relay}/hooks/only-write-reports.sh" || exit 2'


def frontmatter(role: str) -> dict:
    text = (REPO / "agents" / f"{role}.md").read_text()
    return yaml.safe_load(text.split("---\n")[1])


@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_a_read_only_role_has_no_edit_tool(role):
    data = frontmatter(role)
    tools = [t.strip() for t in data["tools"].split(",")]
    assert "Edit" not in tools and "NotebookEdit" not in tools
    assert {"Edit", "NotebookEdit"} <= {t.strip() for t in data["disallowedTools"].split(",")}


@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_a_read_only_role_runs_the_write_guard_on_every_write(role):
    (rule,) = frontmatter(role)["hooks"]["PreToolUse"]
    assert rule["matcher"] == "Write"
    assert rule["hooks"] == [{"type": "command", "command": HOOK_COMMAND}]


@pytest.mark.parametrize("role", ("builder", "tester"))
def test_the_roles_that_change_code_keep_their_tools(role):
    data = frontmatter(role)
    assert "tools" not in data and "hooks" not in data


def guard(relay_home: Path, payload: str, tmpdir: Path) -> subprocess.CompletedProcess:
    env = {"RELAY_HOME": str(relay_home), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmpdir),
           "HOME": str(relay_home.parent)}
    return subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True,
                          env=env)


def skip_if_under_tmp(tmp_path: Path) -> None:
    """The guard lets any write under /tmp through, as scratch. On Linux
    pytest's own folders are under /tmp, so a refusal can't be shown there;
    CI runs pytest with --basetemp outside /tmp for that reason."""
    tmp = os.path.realpath("/tmp")
    if os.path.commonpath([tmp, os.path.realpath(tmp_path)]) == tmp:
        pytest.skip("pytest's temporary folder is under /tmp; run with --basetemp outside it")


def write_to(path) -> str:
    return json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "x"}})


@pytest.fixture
def home(tmp_path):
    relay_home = tmp_path / "relay-home"
    (relay_home / "tasks" / "t1").mkdir(parents=True)
    (tmp_path / "scratch").mkdir()
    return relay_home


def test_the_guard_lets_a_role_write_its_report(home, tmp_path):
    assert guard(home, write_to(home / "tasks" / "t1" / "04-reviewer.md"), tmp_path / "scratch").returncode == 0


def test_the_guard_lets_a_role_write_a_scratch_file(home, tmp_path):
    assert guard(home, write_to(tmp_path / "scratch" / "probe.py"), tmp_path / "scratch").returncode == 0


@pytest.mark.parametrize("where", ["repo/src/app.py", "relay-home/registry.yaml",
                                   "relay-home/tasks/../registry.yaml", "relay-home/tasks"])
def test_the_guard_refuses_every_other_write(home, tmp_path, where):
    skip_if_under_tmp(tmp_path)
    result = guard(home, write_to(tmp_path / where), tmp_path / "scratch")
    assert result.returncode == 2
    assert "read-only" in result.stderr


def test_the_guard_refuses_a_write_through_a_link_out_of_the_tasks_folder(home, tmp_path):
    skip_if_under_tmp(tmp_path)
    (tmp_path / "repo").mkdir()
    (home / "tasks" / "t1" / "escape").symlink_to(tmp_path / "repo")
    result = guard(home, write_to(home / "tasks" / "t1" / "escape" / "app.py"), tmp_path / "scratch")
    assert result.returncode == 2


@pytest.mark.parametrize("payload", ["", "not json", "{}", '{"tool_input": {}}',
                                     '{"tool_input": {"file_path": 7}}'])
def test_the_guard_refuses_a_call_it_cannot_read(home, tmp_path, payload):
    result = guard(home, payload, tmp_path / "scratch")
    assert result.returncode == 2
    assert "could not be read" in result.stderr


def test_setup_installs_the_guard_where_the_roles_look():
    text = (REPO / "setup.sh").read_text()
    assert 'cp "${SCRIPT_DIR}/hooks/"*.sh "${RELAY_HOME}/hooks/"' in text


@pytest.mark.parametrize("name", ["brief.md", "proto.yaml"])
def test_the_guard_refuses_a_brief_or_a_prototype_record_even_in_the_tasks_folder(home, tmp_path, name):
    result = guard(home, write_to(home / "tasks" / "t1" / name), tmp_path / "scratch")
    assert result.returncode == 2


def test_the_guard_refuses_when_python_is_missing(home, tmp_path):
    env = {"RELAY_HOME": str(home), "PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)}
    (tmp_path / "empty").mkdir()
    result = subprocess.run(["/bin/bash", str(HOOK)], input=write_to(home / "tasks" / "t1" / "r.md"),
                            capture_output=True, text=True, env=env)
    assert result.returncode == 2 and "python3 is missing" in result.stderr


def test_the_frontmatter_command_turns_any_failure_into_a_refusal(tmp_path):
    env = {"RELAY_HOME": str(tmp_path / "no-install"), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(["/bin/bash", "-c", HOOK_COMMAND], input="{}", capture_output=True,
                            text=True, env=env)
    assert result.returncode == 2
