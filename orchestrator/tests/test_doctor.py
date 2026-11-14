"""Tests for orchestrator/doctor.py, run against a scratch install folder."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doctor
from doctor import FAIL, OK, WARN


@pytest.fixture
def install(tmp_path):
    """A healthy install: one role prompt, its Claude Code copy, the hook, a
    registry with one real repo. Returns (relay_home, claude_agents_dir)."""
    home, claude_agents = tmp_path / "relay", tmp_path / "claude-agents"
    (home / "agents").mkdir(parents=True)
    (home / "hooks").mkdir()
    claude_agents.mkdir()
    (home / "agents" / "reviewer.md").write_text("reviewer rules\n")
    (claude_agents / "reviewer.md").write_text("reviewer rules\n")
    hook = home / "hooks" / "only-write-reports.sh"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    repo = tmp_path / "my-app"
    repo.mkdir()
    (home / "registry.yaml").write_text(f"repos:\n  my-app: {repo}\n")
    with sqlite3.connect(home / "relay.db") as connection:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
    return home, claude_agents


def statuses(home, claude_agents):
    return {check.name: check.status for check in doctor.run_checks(home, claude_agents)}


class TestHealthyInstall:
    def test_install_checks_pass(self, install):
        found = statuses(*install)
        for name in ("Python", "Role prompts", "Write guard hook", "Registry", "Ledger"):
            assert found[name] == OK, name

    def test_exit_code_is_zero_when_nothing_fails(self, install, monkeypatch, capsys):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert doctor.run_doctor(*install) == 0
        assert "Ready to run a task." in capsys.readouterr().out


class TestProblemsAreNamedWithAFix:
    def test_stale_role_prompt_fails(self, install):
        home, claude_agents = install
        (claude_agents / "reviewer.md").write_text("older rules\n")
        check = doctor.check_role_prompts(home, claude_agents)
        assert check.status == FAIL
        assert "out of date: reviewer.md" in check.detail
        assert "setup.sh" in check.fix

    def test_missing_role_prompt_fails(self, install):
        home, claude_agents = install
        (claude_agents / "reviewer.md").unlink()
        assert "missing: reviewer.md" in doctor.check_role_prompts(home, claude_agents).detail

    def test_hook_that_cannot_run_fails(self, install):
        home, _ = install
        (home / "hooks" / "only-write-reports.sh").chmod(0o644)
        assert doctor.check_write_guard(home).status == FAIL

    def test_missing_command_fails_and_says_why_it_is_needed(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        check = doctor.check_command("claude", "run each role")
        assert check.status == FAIL
        assert "run each role" in check.fix

    def test_empty_registry_warns_with_the_register_command(self, install):
        home, _ = install
        (home / "registry.yaml").write_text("repos: {}\n")
        check = doctor.check_registry(home)
        assert check.status == WARN
        assert "relay register" in check.fix

    def test_registered_repo_that_moved_warns(self, install, tmp_path):
        home, _ = install
        (home / "registry.yaml").write_text(f"repos:\n  gone: {tmp_path / 'nowhere'}\n")
        check = doctor.check_registry(home)
        assert check.status == WARN
        assert "gone" in check.detail

    def test_unreadable_registry_fails(self, install):
        home, _ = install
        (home / "registry.yaml").write_text("repos: [unclosed\n")
        assert doctor.check_registry(home).status == FAIL

    def test_corrupt_ledger_fails(self, install):
        home, _ = install
        (home / "relay.db").write_bytes(b"not a database" * 100)
        assert doctor.check_ledger(home).status == FAIL

    def test_missing_ledger_fails_and_names_the_command_that_makes_it(self, install):
        home, _ = install
        (home / "relay.db").unlink()
        check = doctor.check_ledger(home)
        assert check.status == FAIL
        assert "relay recover --init" in check.fix

    def test_exit_code_is_one_when_anything_fails(self, install, capsys):
        home, claude_agents = install
        (claude_agents / "reviewer.md").unlink()
        assert doctor.run_doctor(home, claude_agents) == 1
        assert "fix: Re-run setup.sh." in capsys.readouterr().out


def test_a_real_ledger_passes(install):
    home, _ = install
    assert doctor.check_ledger(home).status == OK
