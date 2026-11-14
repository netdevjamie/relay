"""`relay doctor`: checks that an install can run a task, and says how to fix
what it finds.

Each check returns a Check: a status, what was found, and (when it is not
ok) the fix. The module reads only the paths it is given, so it loads
without the rest of Relay and tests can point it at a scratch folder.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

HEADER = "relay doctor"
OK, WARN, FAIL = "ok", "warn", "fail"
MIN_PYTHON = (3, 11)


def shown(path: Path | str) -> str:
    """A path as a person would type it: the home folder as ~."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def check_python() -> Check:
    found = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= MIN_PYTHON:
        return Check(OK, "Python", found)
    return Check(FAIL, "Python", f"{found} is too old", "Install Python 3.11 or newer, then re-run setup.sh.")


def check_command(name: str, purpose: str) -> Check:
    path = shutil.which(name)
    if path:
        return Check(OK, name, shown(path))
    return Check(FAIL, name, "not found on PATH", f"Install {name}: Relay uses it to {purpose}.")


def check_role_prompts(relay_home: Path, claude_agents_dir: Path) -> Check:
    """Claude Code runs the copies in ~/.claude/agents, so they must match the
    install. A stale copy means a role works from old rules."""
    installed = sorted((relay_home / "agents").glob("*.md"))
    if not installed:
        return Check(FAIL, "Role prompts", f"none in {relay_home / 'agents'}", "Run setup.sh.")
    missing = [p.name for p in installed if not (claude_agents_dir / p.name).is_file()]
    stale = [p.name for p in installed
             if p.name not in missing and (claude_agents_dir / p.name).read_bytes() != p.read_bytes()]
    if missing or stale:
        problems = ([f"missing: {', '.join(missing)}"] if missing else []) + \
                   ([f"out of date: {', '.join(stale)}"] if stale else [])
        return Check(FAIL, "Role prompts", f"{shown(claude_agents_dir)}: {'; '.join(problems)}", "Re-run setup.sh.")
    return Check(OK, "Role prompts", f"{len(installed)} roles, in step with {shown(claude_agents_dir)}")


def check_write_guard(relay_home: Path) -> Check:
    """The hook that keeps read-only roles writing only their own report."""
    hook = relay_home / "hooks" / "only-write-reports.sh"
    if hook.is_file() and hook.stat().st_mode & 0o111:
        return Check(OK, "Write guard hook", shown(hook))
    return Check(FAIL, "Write guard hook", f"{hook} is missing or not executable", "Re-run setup.sh.")


def check_registry(relay_home: Path) -> Check:
    path = relay_home / "registry.yaml"
    if not path.is_file():
        return Check(FAIL, "Registry", f"{path} not found", "Run setup.sh.")
    try:
        repos = (yaml.safe_load(path.read_text()) or {}).get("repos") or {}
    except (yaml.YAMLError, AttributeError) as error:
        return Check(FAIL, "Registry", f"{path} cannot be read ({error.__class__.__name__})",
                     "Fix the YAML by hand; it maps each alias to a repo path.")
    if not repos:
        return Check(WARN, "Registry", "no repos registered yet",
                     "relay register /path/to/repo --alias my-repo")
    gone = [alias for alias, path_text in repos.items()
            if not Path(str(path_text)).expanduser().is_dir()]
    if gone:
        return Check(WARN, "Registry", f"path not found for: {', '.join(sorted(gone))}",
                     "Register the repo again at its new path.")
    return Check(OK, "Registry", f"{len(repos)} repo(s)")


def check_ledger(relay_home: Path) -> Check:
    path = relay_home / "relay.db"
    if not path.exists():
        return Check(FAIL, "Ledger", f"no ledger at {shown(path)}",
                     "relay recover --init   (setup.sh creates it on a first install)")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as error:
        return Check(FAIL, "Ledger", f"{path} cannot be opened ({error})", "See `relay recover --help`.")
    if result != "ok":
        return Check(FAIL, "Ledger", f"{path} failed its integrity check: {result}",
                     "Restore it from a backup, or see `relay recover`.")
    return Check(OK, "Ledger", shown(path))


def run_checks(relay_home: Path, claude_agents_dir: Path) -> list[Check]:
    return [
        check_python(),
        check_command("git", "read branches and commits"),
        check_command("claude", "run each role"),
        check_role_prompts(relay_home, claude_agents_dir),
        check_write_guard(relay_home),
        check_registry(relay_home),
        check_ledger(relay_home),
    ]


def run_doctor(relay_home: Path, claude_agents_dir: Path) -> int:
    """Print every check. Returns 1 when any check fails, else 0."""
    checks = run_checks(relay_home, claude_agents_dir)
    print(f"{HEADER}: {shown(relay_home)}\n")
    for check in checks:
        print(f"  [{check.status:>4}] {check.name}: {check.detail}")
        if check.fix:
            print(f"         fix: {check.fix}")
    failed = sum(check.status == FAIL for check in checks)
    print()
    print(f"{failed} problem(s) to fix." if failed else "Ready to run a task.")
    return 1 if failed else 0
