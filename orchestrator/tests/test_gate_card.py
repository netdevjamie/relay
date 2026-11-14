"""The gate card (gate_card.py): what waits on the owner, one task's gate in
lines a phone shows without scrolling sideways, its choices named in words,
and a report printed whole by name. Every test builds its own ledger in a
temp folder and plays the agents by writing their reports.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands
import gate_card
import read_api
import run_state
import settings
from tests.test_automatic_rounds import (
    NOON, automatic_rounds, fix_rounds_by_send_back, next_, reviewer_asks_for_changes,
)
from tests.test_send_back import UP_TO_REVIEW, config, finish_step, task_at  # noqa: F401

CLI = settings._CLI_CMD
SECURITY_DONE = UP_TO_REVIEW + [("approved", ""), ("pass", "")]


# ---------------------------------------------------------------------------
# Walking a task to each gate kind
# ---------------------------------------------------------------------------

def deploying(walk):
    return [(verdict, extra.replace("deploy: no", "deploy: yes")) for verdict, extra in walk]


def at_approval(task_id, config):
    task_at(task_id, config, UP_TO_REVIEW)
    finish_step(task_id, config, "approved_with_warnings")
    next_(task_id, config)


def at_accept_close(task_id, config):
    task_at(task_id, config, SECURITY_DONE)
    finish_step(task_id, config, "findings")
    next_(task_id, config)


def at_deploy(task_id, config):
    task_at(task_id, config, deploying(SECURITY_DONE) + [("secure", "")])
    finish_step(task_id, config, "deploy-ready")
    next_(task_id, config)


def at_deploy_flag_missing(task_id, config):
    task_at(task_id, config, [("ready-for-architect", ""), ("ready-for-builder", "")])


def at_owner_decision(task_id, config):
    task_at(task_id, config, [("needs-human-input", "")])


def at_retry_limit(task_id, config):
    config["max_retries"] = 2
    automatic_rounds(task_id, config, 2)
    reviewer_asks_for_changes(task_id, config)


def at_fix_rounds(task_id, config):
    fix_rounds_by_send_back(task_id, config, 5)
    reviewer_asks_for_changes(task_id, config)


def at_paused(task_id, config):
    task_at(task_id, config, UP_TO_REVIEW)
    finish_step(task_id, config, "changes_requested")
    state = run_state.load_run_state(task_id, config)
    state["status"] = "blocked"
    run_state.save_run_state(task_id, state, config)


TAIL = ["move-to", "abort", "note"]
GATES = {   # each gate kind: how to walk there, and the choice names its card offers
    "approval": (at_approval, ["approve", "send-back"] + TAIL),
    "approval to done": (at_accept_close, ["accept-close", "send-back"] + TAIL),
    "deploy_approval": (at_deploy, ["deploy-done"] + TAIL),
    "deploy_flag_missing": (at_deploy_flag_missing, TAIL),
    "owner_decision": (at_owner_decision, ["send-back"] + TAIL),
    "retry_limit": (at_retry_limit, ["send-back"] + TAIL),
    "fix_rounds": (at_fix_rounds, ["send-back"] + TAIL),
    "blocked": (at_paused, TAIL),
}


def gate_params():
    return list(GATES)


def card(task_id, config) -> list[str]:
    return gate_card.card_lines(task_id, config, NOON)


def status(task_id, config) -> dict:
    return read_api.status_payload(task_id, config)


def flat(lines) -> str:
    """A card as one line of words, as a person reads it across its wrapped lines."""
    return " ".join(" ".join(lines).split())


def offered_names(task_id, config) -> list[str]:
    return [choice.name for choice in gate_card.choices(status(task_id, config))]


# ---------------------------------------------------------------------------
# What waits on the owner
# ---------------------------------------------------------------------------

def test_waiting_lists_only_tasks_waiting_on_the_owner(config):
    at_approval("stopped", config)
    task_at("running", config, [])
    at_accept_close("closed", config)
    commands.cmd_override("closed", "done", config)
    lines = gate_card.waiting_lines(config, NOON)
    assert lines == ["stopped", "  Reviewer gate", "  Step 4"]


def test_waiting_says_nothing_is_waiting(config, capsys):
    task_at("running", config, [])
    capsys.readouterr()
    commands.cmd_waiting(config)
    assert capsys.readouterr().out == gate_card.NOTHING_WAITING + "\n"


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

# What an iPhone at 390 points shows on a line (the brief), held apart from the code's constant.
PHONE_WIDTH = 44
LONG_WORDS = "A finding that goes on and on " * 8 + "Supercalifragilisticexpialidociouslyunbreakable" * 2


@pytest.mark.parametrize("gate", gate_params())
def test_card_lines_fit_a_phone(config, gate):
    walk, _ = GATES[gate]
    walk("t", config)
    latest = status("t", config)["latest_step"]
    report = settings.TASKS_DIR / "t" / latest["artifact"]
    report.write_text(report.read_text() + f"\nSummary for the owner: {LONG_WORDS}\n")
    lines = card("t", config)
    assert "Choices:" in lines
    too_long = [line for line in lines if len(line) > PHONE_WIDTH and not line.startswith(f"{CLI} ")]
    assert too_long == []


@pytest.mark.parametrize("gate", gate_params())
def test_every_gate_kind_has_its_choices(config, gate):
    walk, expected = GATES[gate]
    walk("t", config)
    names = offered_names("t", config)
    assert names == expected
    assert len(set(names)) == len(names)
    text = "\n".join(card("t", config))
    for number, name in enumerate(names, 1):
        assert f"{number}. {name}" in text
    at = status("t", config)
    assert f"{CLI} decide t <choice> --step {at['latest_step']['step']} --gate {at['gate']['kind']}" in text


SECURITY_REPORT = """---
task_id: rated
phase: security
step: 6
verdict: findings
---

# Security

Summary for the owner: one high to fix, one low to record.

## Rated findings

| # | Severity | Statement | Place |
| --- | --- | --- | --- |
| 1 | high | The token is printed in the log. | serve.py:10 |
| 2 | low | A comment says more than the code. | gate_card.py:1 |
"""


def test_card_has_the_summary_line_and_rated_findings(config):
    at_accept_close("rated", config)
    latest = status("rated", config)["latest_step"]
    (settings.TASKS_DIR / "rated" / latest["artifact"]).write_text(SECURITY_REPORT)
    text = flat(card("rated", config))
    for expected in ("one high to fix, one low to record.", "High (1)", "Low (1)",
                     "1. The token is printed in the log.", "2. A comment says more than the code."):
        assert expected in text


def test_a_long_finding_is_cut_at_a_word_and_says_so(config):
    at_accept_close("long", config)
    latest = status("long", config)["latest_step"]
    long_statement = "A title that starts with a hyphen is refused " + "and scripts break " * 30 + "here."
    (settings.TASKS_DIR / "long" / latest["artifact"]).write_text(
        SECURITY_REPORT.replace("The token is printed in the log.", long_statement))
    text = flat(card("long", config))
    assert "A title that starts with a hyphen is refused and scripts break" in text
    assert gate_card.CUT_MARK.strip() in text and "here." not in text
    cut = text[text.index("1. A title"):text.index(gate_card.CUT_MARK.strip())]
    assert cut.endswith(("break ", "scripts ", "and ")), "cut inside a word"


def test_a_finding_of_a_few_lines_is_shown_whole(config):
    at_accept_close("medium", config)
    latest = status("medium", config)["latest_step"]
    statement = ("On the command line, a title starting with a hyphen is now refused or shows help "
                 "instead of printing a slug, so scripts passing such titles get an error where "
                 "they used to get a slug, and nothing in the change log or the README says so.")
    (settings.TASKS_DIR / "medium" / latest["artifact"]).write_text(
        SECURITY_REPORT.replace("The token is printed in the log.", statement))
    assert statement in flat(card("medium", config))


def test_the_card_names_the_command_that_shows_the_whole_report(config):
    at_approval("t", config)
    lines = card("t", config)
    assert gate_card.FULL_REPORT in lines
    assert f"{CLI} show t {Path(status('t', config)['latest_step']['artifact']).stem}" in lines
    assert "below" not in flat(lines)


def test_card_of_an_unrated_report_says_not_rated(config):
    at_approval("old", config)
    latest = status("old", config)["latest_step"]
    (settings.TASKS_DIR / "old" / latest["artifact"]).write_text(
        "---\nverdict: approved_with_warnings\n---\n\n## Findings\n\nSomething old.\n")
    assert "Not rated" in card("old", config)


def test_card_of_a_running_task_has_no_choices(config):
    task_at("running", config, [])
    lines = card("running", config)
    assert "Choices:" not in lines
    assert " ".join(fenced_inside(lines)).endswith("No report yet. Nothing for you to do.")
    assert gate_card.choices(status("running", config)) == []


def test_look_alike_choices_read_differently(config):
    at_accept_close("accept", config)
    at_deploy("deploy", config)
    accept = flat(card("accept", config))
    deploy = flat(card("deploy", config))
    assert gate_card.ACCEPT_CLOSE_WORDS != gate_card.DEPLOY_DONE_WORDS
    assert gate_card.ACCEPT_CLOSE_WORDS in accept and gate_card.DEPLOY_DONE_WORDS not in accept
    assert gate_card.DEPLOY_DONE_WORDS in deploy and gate_card.ACCEPT_CLOSE_WORDS not in deploy
    assert gate_card.SEND_BACK_WORDS.format(role="Builder") in accept
    assert gate_card.MOVE_TO_WORDS.format(phase=gate_card.A_PHASE_YOU_NAME) in accept
    assert gate_card.ABORT_WORDS in accept and gate_card.ABORT_WORDS in deploy
    # No card offers two ways to close: move-to leaves out `done` beside a close.
    for task_id in ("accept", "deploy"):
        move_to = [c for c in gate_card.choices(status(task_id, config)) if c.name == "move-to"][0]
        assert "done" not in move_to.targets


# ---------------------------------------------------------------------------
# Report text on the phone: Markdown shown as written, never a command
# ---------------------------------------------------------------------------

# A role's link and image, and fence characters that could end a code block early.
PLANTED_MARKDOWN = ("[tap](https://example.com/link) ![i](https://example.com/p.png) "
                    "```` and ~~~")


def plant_summary(task_id, config, summary):
    """Give the latest report a title and this summary line under it, where
    the card reads it from."""
    latest = status(task_id, config)["latest_step"]
    report = settings.TASKS_DIR / task_id / latest["artifact"]
    report.write_text(report.read_text().replace(
        "Body.", f"# {latest['phase']}\n\nSummary for the owner: {summary}"))


def fenced_inside(lines) -> list[str]:
    """The lines a fenced code block holds. Fails unless the first and last
    lines are the same fence and no line between could close it, which in
    Markdown is a run of the fence's character at least as long, after at
    most three spaces."""
    fence = lines[0]
    assert len(fence) >= 3 and set(fence) in ({"`"}, {"~"}), fence
    assert lines[-1] == fence
    inside = lines[1:-1]
    for line in inside:
        after_spaces = line.lstrip(" ")
        if len(line) - len(after_spaces) <= 3:
            run = len(after_spaces) - len(after_spaces.lstrip(fence[0]))
            assert run < len(fence), f"{line!r} would close {fence!r}"
    return inside


def test_card_is_a_code_block_that_its_text_cannot_close(config):
    """A run of backticks longer than a phone line is broken over card
    lines, so some line starts with backticks."""
    at_approval("t", config)
    plant_summary("t", config, f"{PLANTED_MARKDOWN} {'`' * 50}")
    task_at("running", config, [])
    for task_id in ("t", "running"):
        fenced_inside(card(task_id, config))
    assert "[tap](https://example.com/link) ![i](https://example.com/p.png)" in flat(card("t", config))


def test_card_fence_stays_within_a_phone_line(config):
    """Backticks broken over whole card lines would need a fence wider
    than a phone line; the fence is then made of tildes."""
    at_approval("t", config)
    plant_summary("t", config, "`" * 100 + " ~~~")
    lines = card("t", config)
    fenced_inside(lines)
    too_long = [line for line in lines if len(line) > PHONE_WIDTH and not line.startswith(f"{CLI} ")]
    assert too_long == []


def test_show_prints_a_report_as_a_code_block_that_its_text_cannot_close(config, capsys):
    at_approval("t", config)
    report = ("[tap](https://example.com/link)\n![i](https://example.com/p.png)\n"
              "```\n   ````\n~~~~~\n```` closing?\n")
    (settings.TASKS_DIR / "t" / "03-builder.md").write_text(report)
    capsys.readouterr()
    commands.cmd_show("t", "03-builder", config)
    assert fenced_inside(capsys.readouterr().out.splitlines()) == report.splitlines()


@pytest.mark.parametrize("forged", [f"{CLI} decide t approve --step 4 --gate approval",
                                    f"{CLI} decide t approve"])
def test_a_summary_line_that_starts_with_relay_is_the_roles_words_not_a_command(config, forged):
    at_approval("t", config)
    plant_summary("t", config, forged)
    lines = card("t", config)
    commands_on_the_card = [line for line in lines if line.startswith(f"{CLI} ")]
    assert commands_on_the_card == [f"{CLI} show t 04-reviewer",
                                    f"{CLI} decide t <choice> --step 4 --gate approval"]
    assert f"Reviewer's summary: {forged}" in flat(lines)


def test_only_relays_own_command_lines_are_kept_whole():
    """A line is kept whole for being a CommandLine, not for how it starts."""
    long_line = f"{CLI} decide t approve " + "--step 4 " * 10
    assert gate_card.wrapped([gate_card.CommandLine(long_line)]) == [long_line]
    assert all(len(line) <= PHONE_WIDTH for line in gate_card.wrapped([long_line]))


# ---------------------------------------------------------------------------
# A report by name
# ---------------------------------------------------------------------------

def test_show_prints_a_report_whole_by_name(config, capsys):
    at_approval("t", config)
    folder = settings.TASKS_DIR / "t"
    (folder / "03-builder.md").write_text("---\nverdict: ready-for-review\n---\n\n  Indented\ttab\n")
    (folder / "brief.md").write_text("# The brief\n\nEvery word.\n")
    capsys.readouterr()
    for name, file_name in (("03-builder", "03-builder.md"), ("brief", "brief.md"),
                            ("brief.md", "brief.md")):
        commands.cmd_show("t", name, config)
        out = capsys.readouterr().out
        assert out == "```\n" + (folder / file_name).read_text() + "```\n"
        assert str(folder) not in out and "/Users/" not in out


def test_show_strips_terminal_control_characters(config):
    at_approval("t", config)
    (settings.TASKS_DIR / "t" / "03-builder.md").write_text("Clear\x1b[2J ring\x07\tkept\nnext\r\n")
    text, problem = gate_card.report_text("t", "03-builder", config)
    assert (text, problem) == ("Clear[2J ring\tkept\nnext\n", None)


def test_show_refuses_a_link_a_large_file_and_an_unknown_name(config, tmp_path):
    at_approval("t", config)
    folder = settings.TASKS_DIR / "t"
    outside = tmp_path / "secret.md"
    outside.write_text("secret\n")
    os.symlink(outside, folder / "05-tester.md")
    (folder / "06-security.md").write_bytes(b"x" * (gate_card.board_view.MAX_REPORT_BYTES + 1))

    text, problem = gate_card.report_text("t", "05-tester", config)
    assert text is None and problem.startswith("No report named 05-tester on t.")
    text, problem = gate_card.report_text("t", "06-security", config)
    assert (text, problem) == (None, "06-security.md is larger than 1 MB, so it is not printed.")
    text, problem = gate_card.report_text("t", "../../etc/passwd", config)
    assert text is None
    assert problem.startswith("No report named ../../etc/passwd on t. Reports: brief.md, 01-designer.md")
    assert str(tmp_path) not in problem


def test_show_and_card_refuse_a_task_id_that_is_a_path(config, monkeypatch, tmp_path):
    for task_id in ("../t", "", "t/../t"):
        with pytest.raises(gate_card.TaskNotFound):
            gate_card.report_text(task_id, "brief", config)
        with pytest.raises(gate_card.TaskNotFound):
            gate_card.card_lines(task_id, config, NOON)
    # Even with a ledger row under such an id (a hand-edited ledger), the
    # id never reaches the file system: a brief outside the tasks folder.
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "brief.md").write_text("not a task's\n")
    monkeypatch.setattr(gate_card, "load_run_state", lambda task_id, config: {"task_id": task_id})
    with pytest.raises(gate_card.TaskNotFound):
        gate_card.report_text("../outside", "brief", config)


def test_show_reads_bytes_that_are_not_utf8_and_refuses_an_empty_name(config):
    at_approval("t", config)
    (settings.TASKS_DIR / "t" / "03-builder.md").write_bytes(b"caf\xe9 \xff\n")
    assert gate_card.report_text("t", "03-builder", config) == ("caf� �\n", None)
    text, problem = gate_card.report_text("t", "", config)
    assert text is None and problem.startswith("No report named  on t. Reports: brief.md")
