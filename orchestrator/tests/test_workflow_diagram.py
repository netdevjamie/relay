"""README.md and docs/workflow.mmd draw the same chain, and it holds the
gates and the loops back to the Builder that routing.py has."""
import re
from pathlib import Path

import routing

REPO = Path(__file__).resolve().parents[2]


def readme_diagram() -> str:
    found = re.search(r"```mermaid\n(.*?)```", (REPO / "README.md").read_text(), re.S)
    assert found, "README.md has no mermaid diagram"
    return found.group(1)


def file_diagram() -> str:
    lines = (REPO / "docs" / "workflow.mmd").read_text().splitlines(keepends=True)
    return "".join(line for line in lines if not line.startswith("%%"))


def test_the_readme_and_the_diagram_file_are_the_same():
    assert readme_diagram() == file_diagram()


def test_every_role_that_can_send_work_back_has_an_edge_to_the_builder():
    sends_back = {phase for phase, verdicts in routing.ROUTING.items()
                  for next_phase, _ in verdicts.values() if next_phase == "builder"}
    nodes = {"reviewer": "R", "tester": "Te", "security": "S"}
    for phase in sends_back & set(nodes):
        assert re.search(rf"^\s*{nodes[phase]} -->\|[^|]+\| B$", file_diagram(), re.M), (
            f"the diagram has no edge from the {phase} back to the Builder")


def test_the_plan_gate_is_drawn():
    assert "A -->|you approve the plan| B" in file_diagram()
