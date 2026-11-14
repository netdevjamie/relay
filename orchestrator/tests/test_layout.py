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
LAYERS = ["settings", "sizing", "proto", "run_state", "routing", "agents", "recorded_runs",
          "owner_notes", "read_api", "board_view", "gate_card", "commands", "relay"]
# These load without any of the layered modules.
STANDALONE = ["db", "evidence", "project_profile", "doctor", "awake", "markdown_blocks",
              "report_reader", "bounded_yaml"]

# Replaced at run time by tests, or rebound with `global`. A by-value copy
# (`from settings import TASKS_DIR`) would never see the replacement.
READ_THROUGH_MODULE = {
    "settings": {"TASKS_DIR", "REGISTRY_PATH", "CONFIG_PATH", "_db_connection",
                 "load_config", "_db_path"},
    "read_api": {"read_gate"},
    "agents": {"invoke_agent"},
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
