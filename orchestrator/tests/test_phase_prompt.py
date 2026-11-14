"""What the prompt Relay prints for a phase says about rating findings.

The role file is the one home of the rating scale and the verdict rule.
The printed prompt carries one sentence for the phases that rate findings,
in the role file's own words, and nothing for any other phase.
"""
import re

import pytest

import agents
import report_reader
import settings
from tests.test_agent_prompts import RATING_ROLE_FILES, ROLE_FILES, defined_ratings

RATING_PHASES = [role_file.removesuffix(".md") for role_file in RATING_ROLE_FILES]
OTHER_PHASES = [role_file.removesuffix(".md") for role_file in ROLE_FILES
                if role_file not in RATING_ROLE_FILES]
# Words other scales use for a rating. None of them may reach a role through
# the prompt, where it would read as a second scale.
OTHER_RATING_WORDS = ("nit", "nits", "info", "blocker", "critical", "warning", "concern")


def printed_prompt(phase: str, tasks_dir, monkeypatch) -> str:
    monkeypatch.setattr(settings, "TASKS_DIR", tasks_dir)
    return agents.build_prompt(phase, "a-task", 4, f"04-{phase}.md")


def whole_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


@pytest.mark.parametrize("phase", RATING_PHASES)
def test_rating_phase_is_told_the_three_ratings(phase, tmp_path, monkeypatch):
    line = agents.rating_line(phase)
    named = re.findall(r"`([^`]+)`", line)
    assert named == list(report_reader.SEVERITIES)
    assert set(named) <= set(defined_ratings(f"{phase}.md"))
    assert "highest" in line and "verdict" in line
    assert f"\n\n{line}" in printed_prompt(phase, tmp_path, monkeypatch)


@pytest.mark.parametrize("phase", RATING_PHASES)
def test_rating_phase_prompt_names_no_other_rating_word(phase, tmp_path, monkeypatch):
    prompt = printed_prompt(phase, tmp_path, monkeypatch)
    assert not whole_words(prompt) & set(OTHER_RATING_WORDS)


@pytest.mark.parametrize("phase", OTHER_PHASES)
def test_other_phases_are_told_nothing_about_ratings(phase, tmp_path, monkeypatch):
    assert agents.rating_line(phase) == ""
    prompt = printed_prompt(phase, tmp_path, monkeypatch)
    assert not whole_words(prompt) & set(report_reader.SEVERITIES)
