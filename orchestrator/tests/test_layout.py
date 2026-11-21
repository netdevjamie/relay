"""The module layout holds: every module imports cold, modules import only
from lower layers, and names that tests replace are never imported by value.

relay.py was split into these modules as a pure move. These checks keep the
two things that made the move safe from quietly rotting.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ORCHESTRATOR = Path(__file__).resolve().parent.parent

# Lowest layer first. A module may import only from modules before it.
LAYERS = ["settings", "sizing", "proto", "run_state", "base_trust", "arbiter", "routing", "role_context", "agents",
          "recorded_runs", "risk_record", "owner_notes", "arbiter_record", "read_api", "board_view",
          "gate_card", "commands", "relay"]
# These load without any of the layered modules.
STANDALONE = ["db", "migrate_legacy", "evidence", "project_profile", "doctor", "awake", "markdown_blocks", "printable",
              "scratch_checkout", "seen_to_fail", "test_harness",
              "report_reader", "bounded_yaml", "risk_hint"]

# Replaced at run time by tests, or rebound with `global`. A by-value copy
# (`from settings import TASKS_DIR`) would never see the replacement.
READ_THROUGH_MODULE = {
    "settings": {"TASKS_DIR", "REGISTRY_PATH", "CONFIG_PATH", "_db_connection",
                 "load_config", "_db_path"},
    "read_api": {"read_gate"},
    "agents": {"invoke_agent", "run_role"},
}


def top_level_imports(module: str) -> list[ast.stmt]:
    tree = ast.parse((ORCHESTRATOR / f"{module}.py").read_text())
    return [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]


@pytest.mark.parametrize("module", LAYERS + STANDALONE)
def test_module_imports_in_a_fresh_interpreter(module):
    """Tests import everything in one fixed order, which would hide a module
    that only loads because another was loaded first."""
    done = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ORCHESTRATOR,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("module", LAYERS)
def test_module_imports_only_from_lower_layers(module):
    allowed = set(LAYERS[:LAYERS.index(module)])
    for node in top_level_imports(module):
        names = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
        for name in names:
            if name in LAYERS:
                assert name in allowed, f"{module}.py imports {name}, which is not a lower layer"


@pytest.mark.parametrize("module", STANDALONE)
def test_standalone_modules_import_no_layered_module_when_loaded(module):
    for node in top_level_imports(module):
        names = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
        assert not set(names) & set(LAYERS), f"{module}.py imports {names} at load time"


@pytest.mark.parametrize("module", LAYERS)
def test_replaceable_names_are_never_imported_by_value(module):
    tree = ast.parse((ORCHESTRATOR / f"{module}.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in READ_THROUGH_MODULE:
            copied = {alias.name for alias in node.names} & READ_THROUGH_MODULE[node.module]
            assert not copied, (f"{module}.py copies {sorted(copied)} from {node.module}; "
                                f"read them as {node.module}.NAME")


def test_every_orchestrator_module_is_listed():
    on_disk = {path.stem for path in ORCHESTRATOR.glob("*.py")}
    assert on_disk == set(LAYERS) | set(STANDALONE)


def test_the_ledger_never_names_agents():
    """db.py records the report file it is handed at dispatch; it never
    works one out, so it has no reason to import agents (not even late,
    inside a function) or to name artifact_name."""
    source = (ORCHESTRATOR / "db.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert "agents" not in {alias.name for alias in node.names}, "db.py imports agents"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "agents", "db.py imports from agents"
    assert "artifact_name" not in source


def test_commands_record_steps_retries_and_verdicts_only_through_run_state():
    """A step, an automatic round and a verdict are recorded by
    run_state.dispatch_step, count_retry and record_verdict. The in-memory
    helpers alone record nothing in the ledger, so a command that called
    one and then saved would silently lose the step."""
    source = (ORCHESTRATOR / "commands.py").read_text()
    for in_memory_only in ("record_phase(", "increment_retry(", "_record_verdict_in_history(",
                           "record_step_and_save"):
        assert in_memory_only not in source, in_memory_only


def _function_bodies(tree: ast.Module) -> list[ast.AST]:
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _names_used(nodes: list[ast.AST]) -> set[str]:
    used = set()
    for node in nodes:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name):
                used.add(inner.id)
            elif isinstance(inner, ast.Attribute):
                used.add(inner.attr)
    return used


def _strings_used(tree: ast.AST) -> set[str]:
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _event_constants(group: frozenset) -> dict[str, str]:
    """{constant name: event type} for one of db's event groups."""
    import db
    return {name: getattr(db, name) for name in dir(db)
            if name.startswith("EVENT_") and getattr(db, name) in group}


def test_only_migrate_legacy_writes_migration_or_legacy_event_types():
    """Migration-only event types are written only by migrate_legacy.py;
    legacy ones by nothing at all. Every other module names neither the
    constants nor their values, and db.py names them only where it
    defines them, never inside a function."""
    import db
    migration = _event_constants(db.MIGRATION_EVENT_TYPES)
    legacy = _event_constants(db.LEGACY_EVENT_TYPES)
    assert set(migration.values()) == db.MIGRATION_EVENT_TYPES
    assert set(legacy.values()) == db.LEGACY_EVENT_TYPES
    group_names = {"MIGRATION_EVENT_TYPES", "LEGACY_EVENT_TYPES"}
    for path in ORCHESTRATOR.glob("*.py"):
        tree = ast.parse(path.read_text())
        if path.stem == "db":
            used_in_functions = _names_used(_function_bodies(tree))
            assert not used_in_functions & (set(migration) | set(legacy) | group_names)
            continue
        names, strings = _names_used([tree]), _strings_used(tree)
        forbidden = dict(legacy)
        if path.stem != "migrate_legacy":
            forbidden.update(migration)
        assert not names & (set(forbidden) | group_names), path.name
        assert not strings & set(forbidden.values()), path.name
