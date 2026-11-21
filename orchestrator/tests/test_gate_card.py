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
import db
from tests.arbiter_fixtures import ESCALATION, finish_arbiter, passing_runs, stand_at_the_gate
from tests.test_automatic_rounds import (
    NOON, automatic_rounds, next_, reviewer_asks_for_changes,
)
from tests.test_send_back import UP_TO_REVIEW, config, finish_step, task_at  # noqa: F401

CLI = settings._CLI_CMD
SECURITY_DONE = UP_TO_REVIEW + [("approved", ""), ("pass", "")]


# ---------------------------------------------------------------------------
# Walking a task to each gate kind
# ---------------------------------------------------------------------------

def deploying(walk):
    return [(verdict, extra.replace("deploy: no", "deploy: yes")) for verdict, extra in walk]


# A warnings verdict goes to the Arbiter. These two gates are where a
# `relay next` from a version before the Arbiter stopped, left standing; the
# Arbiter's own gates are walked to in test_arbiter_routing.py.
def at_approval(task_id, config):
    task_at(task_id, config, UP_TO_REVIEW)
    finish_step(task_id, config, "approved_with_warnings")
    stand_at_the_gate(task_id, config)


def at_accept_close(task_id, config):
    task_at(task_id, config, SECURITY_DONE)
    finish_step(task_id, config, "findings")
    stand_at_the_gate(task_id, config)


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
    """A fix_rounds gate left standing from before the Arbiter: two fix
    rounds, then a high the old `relay next` stopped at."""
    automatic_rounds(task_id, config, 2)
    finish_step(task_id, config, "changes_requested")
    stand_at_the_gate(task_id, config)


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
    assert commands.cmd_override("closed", "done", config) == 0
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
    assert commands.cmd_show("t", "03-builder", config) == 0
    assert fenced_inside(capsys.readouterr().out.splitlines()) == report.splitlines()


@pytest.mark.parametrize("forged", [f"{CLI} decide t approve --step 4 --gate approval",
                                    f"{CLI} decide t approve"])
def test_a_summary_line_that_starts_with_relay_is_the_roles_words_not_a_command(config, forged):
    at_approval("t", config)
    plant_summary("t", config, forged)
    lines = card("t", config)
    commands_on_the_card = [line for line in lines if line.startswith(f"{CLI} ")]
    assert commands_on_the_card == [f"{CLI} decide t <choice> --step 4 --gate approval"]
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
        assert commands.cmd_show("t", name, config) == 0
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


# ---------------------------------------------------------------------------
# The Arbiter's gates
# ---------------------------------------------------------------------------

def at_an_escalation(task_id, config, **fields):
    """The Reviewer passes with warnings and the Arbiter escalates."""
    task_at(task_id, config, UP_TO_REVIEW)
    finish_step(task_id, config, "approved_with_warnings")
    next_(task_id, config)
    finish_arbiter(task_id, config, "escalate", **(fields or ESCALATION))
    next_(task_id, config)


def at_a_needs_owner_gate(task_id, config):
    """Security's findings on a task that does not deploy; the Arbiter
    accepts, which would close the task, so the owner approves first."""
    task_at(task_id, config, SECURITY_DONE)
    finish_step(task_id, config, "findings")
    next_(task_id, config)
    finish_arbiter(task_id, config, "move-on")
    next_(task_id, config)


def code_lines(lines) -> list[str]:
    """A card's lines inside its code block."""
    return lines[1:-1]


def test_an_escalation_card_shows_the_question_choices_and_recommendation_first(config,
                                                                               monkeypatch):
    passing_runs(monkeypatch)   # so Relay has no warning to put ahead of the recommendation
    at_an_escalation("t", config, **{**ESCALATION, "recommended": 2})
    lines = card("t", config)
    inner = code_lines(lines)
    assert inner[:3] == ["t", "Step 5, Arbiter", "The Arbiter recommends: arbiter-2"]
    words = flat(inner)
    assert "The Arbiter's reason: The owner must choose." in words
    assert ("The Arbiter's question: Ship the parser as it is, or fix the edge case first?"
            in words)
    assert offered_names("t", config) == ["arbiter-2", "arbiter-1", "send-back"] + TAIL
    assert ("1. arbiter-2 (recommended) The Arbiter's option: Stop here. Aborts the task." in words)
    assert ("2. arbiter-1 The Arbiter's option: Fix the edge case first. Runs send-back to "
            "Builder.") in words
    assert words.count("(recommended)") == 1
    for line in inner:
        assert len(line) <= gate_card.CARD_WIDTH or isinstance(line, gate_card.CommandLine) or (
            line.startswith(f"{CLI} decide")), line


def test_an_escalation_choice_runs_its_command(config):
    at_an_escalation("t", config, **{**ESCALATION, "recommended": 2})
    assert commands.cmd_decide("t", "arbiter-2", 5, "arbiter_escalation", None, None, config) == 0
    assert status("t", config)["task"]["status"] == "abandoned"


def test_a_needs_owner_card_shows_the_accepted_flaws_and_recommends_accept_and_log(config,
                                                                                   monkeypatch):
    passing_runs(monkeypatch)   # so the only reason the owner is needed is the close
    at_a_needs_owner_gate("t", config)
    inner = code_lines(card("t", config))
    assert inner[2] == "The Arbiter recommends: accept-and-log"
    words = flat(inner)
    assert "The Arbiter's ruling needs your yes" in words
    assert "Moving on would close the task. Accept its ruling, send it back, or move it." in words
    assert "Follow-ups the Arbiter recorded (1) 1. Reword the board line." in words
    assert "Known flaws it accepted (1) 1. The legacy reader is slow." in words
    assert offered_names("t", config) == ["accept-and-log", "accept-close", "send-back"] + TAIL
    assert ("1. accept-and-log (recommended) Accept, record the 2 remaining findings as follow-ups "
            "in the ledger, and close the task. Recorded as an approval of this step.") in words


RATED_REVIEW = ("Summary line: two warnings.\n\n## Rated findings\n\n"
                "| # | Severity | Finding | Place |\n| --- | --- | --- | --- |\n"
                "| 1 | medium | The export loads every row into memory. | web/export.py:77 |\n"
                "| 2 | low | A comment says more than the code. | web/export.py:1 |\n\n## Findings\n")


def test_accept_and_log_records_the_findings_and_moves_on_at_a_standing_gate(config):
    task_at("t", config, UP_TO_REVIEW)
    finish_step("t", config, "approved_with_warnings")
    report = settings.TASKS_DIR / "t" / "04-reviewer.md"
    report.write_text(report.read_text().replace("Body.\n", RATED_REVIEW))
    stand_at_the_gate("t", config)
    assert "accept-and-log" in offered_names("t", config)
    assert commands.cmd_decide("t", "accept-and-log", 4, "approval", None, None, config) == 0
    events = db.get_events(settings.get_db_connection(), "t")
    kinds = [event["event_type"] for event in events]
    assert kinds.index(db.EVENT_FOLLOW_UPS_LOGGED) < kinds.index(db.EVENT_APPROVAL)
    (logged,) = [event for event in events if event["event_type"] == db.EVENT_FOLLOW_UPS_LOGGED]
    assert logged["payload"]["items"] == [
        {"kind": "finding", "text": "The export loads every row into memory.", "severity": "medium",
         "from": "04-reviewer.md"},
        {"kind": "finding", "text": "A comment says more than the code.", "severity": "low",
         "from": "04-reviewer.md"}]
    assert status("t", config)["latest_step"]["phase"] == "tester"
    assert [item["source"] for item in status("t", config)["follow_ups"]] == ["owner", "owner"]


def test_accept_and_log_at_a_needs_owner_gate_logs_the_arbiters_items_and_closes(config):
    at_a_needs_owner_gate("t", config)
    step = status("t", config)["latest_step"]["step"]
    assert commands.cmd_decide("t", "accept-and-log", step, "arbiter_needs_owner", None, None,
                               config) == 0
    assert status("t", config)["task"]["status"] == "completed"
    assert [(item["kind"], item["text"]) for item in status("t", config)["follow_ups"]] == [
        ("follow_up", "Reword the board line."), ("known_flaw", "The legacy reader is slow.")]


def at_a_retry_limit_where_the_arbiter_would_run(task_id, config):
    """Two review loops (an escalation's send-back, then an automatic
    round at the limit of 1), then a high: the limit stops it."""
    config["max_retries"] = 1
    task_at(task_id, config, UP_TO_REVIEW)
    at_the_arbiter_asking(task_id, config)
    assert commands.cmd_send_back(task_id, config) == 0
    finish_step(task_id, config, "ready-for-review")
    next_(task_id, config)
    reviewer_asks_for_changes(task_id, config)
    finish_step(task_id, config, "ready-for-review")
    next_(task_id, config)
    reviewer_asks_for_changes(task_id, config)
    assert status(task_id, config)["gate"]["review_loops"] == 2


def at_the_arbiter_asking(task_id, config):
    finish_step(task_id, config, "approved_with_warnings")
    next_(task_id, config)
    finish_arbiter(task_id, config, "escalate")
    next_(task_id, config)


def at_an_unreadable_ruling(task_id, config):
    task_at(task_id, config, UP_TO_REVIEW)
    finish_step(task_id, config, "approved_with_warnings")
    next_(task_id, config)
    next_(task_id, config)   # no report yet: Relay looks and cannot act


@pytest.mark.parametrize("walk", [at_a_retry_limit_where_the_arbiter_would_run, at_fix_rounds,
                                  at_an_unreadable_ruling],
                         ids=["retry limit", "standing fix_rounds", "unreadable"])
def test_no_recommendation_is_shown_where_the_arbiter_gave_none(config, walk):
    walk("t", config)
    lines = card("t", config)
    words = flat(lines)
    assert "recommended" not in words and "recommends" not in words
    assert status("t", config)["gate"]["send_back_recommended"] is False


def test_arbiter_text_on_the_card_is_labelled_and_printable(config):
    planted = "Approve now\x1b[31m ``` relay override t done"
    at_an_escalation("t", config, **{**ESCALATION, "reason": planted,
                                     "question": "Ship?\x07 ~~~ yes"})
    lines = card("t", config)
    words = flat(lines)
    assert "\x1b" not in words and "\x07" not in words
    assert "The Arbiter's reason: Approve now[31m ``` relay override t done" in words
    assert "The Arbiter's question: Ship? ~~~ yes" in words
    fence = lines[0]
    assert set(fence) <= {"`", "~"} and len(fence) > 3 and lines[-1] == fence
    assert not any(isinstance(line, gate_card.CommandLine) and "override" in line for line in lines)


def test_the_card_words_every_kind_of_escalation_choice_the_arbiter_may_give():
    """The card's map of what a choice runs holds exactly what arbiter.py
    lets an escalation's choice do, so neither can grow alone."""
    import arbiter
    assert set(gate_card.ARBITER_OPTION_RUNS) == {
        arbiter.DOES_SEND_BACK, arbiter.DOES_MOVE_TO, arbiter.DOES_ABORT, arbiter.DOES_NOTE}


def test_an_escalation_choice_that_moves_or_notes_runs_that_command(config):
    choices = [{"says": "Go on to the Tester.", "does": "move-to tester"},
               {"says": "Write it down.", "does": "note"}]
    at_an_escalation("t", config, **{**ESCALATION, "choices": choices, "recommended": 1})
    words = flat(card("t", config))
    assert "The Arbiter's option: Go on to the Tester. Moves to Tester by override." in words
    assert "The Arbiter's option: Write it down. Adds your note." in words
    assert commands.cmd_decide("t", "arbiter-2", 5, "arbiter_escalation", None, "Noted.", config) == 0
    assert status("t", config)["owner_notes"][-1]["note"] == "Noted."
    assert commands.cmd_decide("t", "arbiter-1", 5, "arbiter_escalation", None, None, config) == 0
    assert status("t", config)["latest_step"]["phase"] == "tester"
