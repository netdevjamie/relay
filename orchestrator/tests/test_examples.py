"""The sample app in examples/slugify is the README's quick start, so it has
to work on a machine that has Python and nothing else installed."""
import ast
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import project_profile

SAMPLE_APP = Path(__file__).resolve().parents[2] / "examples" / "slugify"


def sample_test_command() -> str:
    report = project_profile.load_profile(SAMPLE_APP)
    assert report.valid, report.errors
    return report.profile["commands"]["test"]


def modules_imported_by(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_the_sample_test_command_runs_a_standard_library_module():
    words = shlex.split(sample_test_command())
    assert words[:2] == ["python3", "-m"], words
    assert words[2] in sys.stdlib_module_names, (
        f"the sample's test command runs {words[2]}, which a stock python3 does not have")


def test_the_sample_imports_only_the_standard_library_and_its_own_files():
    own_modules = {path.stem for path in SAMPLE_APP.glob("*.py")}
    for path in SAMPLE_APP.glob("*.py"):
        outside = modules_imported_by(path) - own_modules - set(sys.stdlib_module_names)
        assert not outside, f"{path.name} imports {sorted(outside)}, which needs installing"


def test_the_sample_test_command_passes_in_a_copy_of_the_sample(tmp_path):
    copy = tmp_path / "slugify"
    shutil.copytree(SAMPLE_APP, copy, ignore=shutil.ignore_patterns("__pycache__"))
    words = shlex.split(sample_test_command())
    run = subprocess.run([sys.executable, "-I", *words[1:]], cwd=copy,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Ran 5 tests" in run.stderr
