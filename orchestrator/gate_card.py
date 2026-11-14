"""The gate card: one task's gate as short lines a phone shows without
scrolling sideways, each choice named in words the owner can say back.

`relay waiting`, `relay card`, `relay show` and `relay decide` print what
this module gives. Every fact comes from the read API and the shared
wording in board_view, so the card can never answer differently from
`relay list --json`. It renders only and writes nothing.

Reports, notes and task ids are untrusted text: every line leaves through
printable, and a report printed whole loses its control
characters first. The Claude app shows what a session prints as Markdown,
so a card and a report are printed inside a code block their text cannot
close, where Markdown shows a role's link or image as the characters it is
written in.
"""

import re
import textwrap
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import board_view
import read_api
import settings
from read_api import GATE_WAITING_ON_OWNER, TaskNotFound
from routing import GATE_DEPLOY_APPROVAL, GATE_DEPLOY_FLAG_MISSING, GATE_RETRY_LIMIT, send_back_target
from run_state import _is_agent_artifact, load_run_state
from settings import PIPELINE_ORDER, _CLI_CMD

# An iPhone at 390 points shows about this many characters of the Claude
# app's text on a line. A command line is the one kind allowed to be longer.
CARD_WIDTH = 44
# A line of a card that is a report's own words is cut at this many characters.
QUOTED_LIMIT = 160
# A finding is cut later, at a word, and the cut is marked, since the owner
# decides on it from the card.
FINDING_LIMIT = 400
CUT_MARK = " [...]"
# A line Relay writes itself (the ask) is cut only here.
SENTENCE_LIMIT = 4000
# The shortest fence Markdown reads as opening a code block.
SHORTEST_FENCE = 3

NOTHING_WAITING = "Nothing is waiting on you."
STOP_LINE = "Relay is waiting on you: decide before this task can go on."

# What each choice does, in words. Look-alike decisions read differently.
ACCEPT_CLOSE_WORDS = "Accept the findings and close the task. Nothing is deployed."
DEPLOY_DONE_WORDS = "Close the task: you have deployed it and the smoke test passed."
SEND_BACK_WORDS = "Send back to {role} for another round. Recorded as a send-back, not an approval."
MOVE_TO_WORDS = "Move to {phase} by override. Recorded as an approval of this step."
MOVE_TO_DONE_WORDS = "Close the task by override. Recorded as an approval. Nothing is deployed."
ABORT_WORDS = "Abort the task. It closes without being finished."
APPROVE_WORDS = "Approve this step and move on to {next}."
NOTE_WORDS = "Add a note for every later agent. Moves nothing."
NOTE_ADDED_WORDS = "A note for every later agent. Nothing moved."
FULL_REPORT = "Full report:"
A_PHASE_YOU_NAME = "a phase you name (--to)"

# What a choice runs: the CLI command of the same name.
OVERRIDE, SEND_BACK, ABORT, NOTE = "override", "send_back", "abort", "note"

BRIEF = "brief.md"


def printable(text, limit: int = QUOTED_LIMIT) -> str:
    """The text on one line, without any character a terminal would act on
    or not show, cut at `limit` characters. Report text and file names are
    written by agents and by branches, so nothing of theirs is printed
    without passing through here."""
    one_line = " ".join(str(text).split())
    return "".join(character for character in one_line if character.isprintable())[:limit]


@dataclass(frozen=True)
class Choice:
    """One decision on a card. `targets` are the phases an override may
    move to: one for approve and the closes, every phase for move-to."""
    name: str
    words: str
    action: str
    targets: tuple[str, ...] = ()


def choices(status: dict) -> list[Choice]:
    """The choices at the task's gate, in the order the card shows them,
    from the gate the read API gives. None when nothing waits on the owner."""
    gate = status["gate"]
    if gate["state"] != GATE_WAITING_ON_OWNER:
        return []
    kind, next_phase = gate["kind"], gate["next_phase"]
    offered = []
    if gate["approve_command"]:
        if kind == GATE_DEPLOY_APPROVAL:
            offered.append(Choice("deploy-done", DEPLOY_DONE_WORDS, OVERRIDE, ("done",)))
        elif next_phase == "done":
            offered.append(Choice("accept-close", ACCEPT_CLOSE_WORDS, OVERRIDE, ("done",)))
        else:
            offered.append(Choice("approve", APPROVE_WORDS.format(next=board_view.role_title(next_phase)),
                                  OVERRIDE, (next_phase,)))
    if gate["send_back_command"]:
        role = next_phase if kind == GATE_RETRY_LIMIT else send_back_target(status["latest_step"]["phase"])
        offered.append(Choice("send-back", SEND_BACK_WORDS.format(role=board_view.role_title(role)),
                              SEND_BACK, (role,)))
    # One way to close on a card: move-to leaves out `done` where a close is offered.
    closes = any(choice.targets == ("done",) for choice in offered)
    phases = tuple(PIPELINE_ORDER) + (() if closes else ("done",))
    offered += [Choice("move-to", MOVE_TO_WORDS.format(phase=A_PHASE_YOU_NAME), OVERRIDE, phases),
                Choice("abort", ABORT_WORDS, ABORT),
                Choice("note", NOTE_WORDS, NOTE)]
    return offered


def decided_words(choice: Choice, to_phase: str | None) -> str:
    """A choice's words once made: move-to names the phase it moved to."""
    if choice.name == "note":
        return NOTE_ADDED_WORDS
    if choice.name != "move-to":
        return choice.words
    if to_phase == "done":
        return MOVE_TO_DONE_WORDS
    return MOVE_TO_WORDS.format(phase=board_view.role_title(to_phase))


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

class CommandLine(str):
    """A command Relay gives on a card, kept whole so it can be copied.
    Only Relay makes one: text from a report is never one, whatever it
    starts with."""


def wrapped(lines: list[str]) -> list[str]:
    """Every line cut to CARD_WIDTH at spaces, keeping an indent. A
    CommandLine is kept whole, so it can be copied."""
    out = []
    for line in lines:
        if isinstance(line, CommandLine) or not line.strip():
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        out += textwrap.wrap(printable(line, SENTENCE_LIMIT), width=CARD_WIDTH,
                             initial_indent=indent, subsequent_indent=indent,
                             break_long_words=True, break_on_hyphens=False)
    return out


def as_code_block(lines: list[str]) -> list[str]:
    """The lines inside a fenced code block, which Markdown shows as
    written: no link, no image. Only a run of the fence's character at
    least as long can close a fence, so the fence is one longer than the
    longest run in the lines. Backticks or tildes, whichever is shorter,
    so a card's fence fits a phone line unless the card holds a full
    line's run of both."""
    fences = [character * max(SHORTEST_FENCE, _longest_run(lines, character) + 1)
              for character in "`~"]
    fence = min(fences, key=len)
    return [fence, *lines, fence]


def _longest_run(lines: list[str], character: str) -> int:
    runs = re.findall(f"{re.escape(character)}+", "\n".join(lines))
    return max(map(len, runs), default=0)


def row_of(status: dict) -> dict:
    """The task as a `relay list --json` row, which board_view.row_line words."""
    gate, task = status["gate"], status["task"]
    rounds = (status["retries"].get(gate["next_phase"], 0) if gate["kind"] == GATE_RETRY_LIMIT
              else None)
    return {**task, "gate_state": gate["state"], "gate_kind": gate["kind"],
            "automatic_rounds": rounds, "fix_rounds": (gate.get("fix_rounds") or {}).get("count"),
            "latest_step": status["latest_step"]}


def waiting_lines(config: dict, now: datetime) -> list[str]:
    """Every task waiting on the owner, newest first: its id, why it
    stopped and its step. One sentence when there is none."""
    rows = [row for row in read_api.list_payload(config)["tasks"]
            if row["gate_state"] == GATE_WAITING_ON_OWNER]
    if not rows:
        return [NOTHING_WAITING]
    lines = []
    for row in board_view._newest_first(rows, now):
        step = (row["latest_step"] or {}).get("step")
        lines += [row["task_id"], f"  {board_view.row_line(row, now)}", f"  Step {step}", ""]
    return wrapped(lines[:-1])


def cut_at_a_word(text, limit: int) -> str:
    """The text on one line, as printable gives it, cut at the last word
    that fits in `limit` characters, with CUT_MARK where it was cut."""
    whole = printable(text, SENTENCE_LIMIT)
    if len(whole) <= limit:
        return whole
    kept = whole[:limit - len(CUT_MARK)]
    if " " in kept:
        kept = kept[:kept.rindex(" ")]
    return kept + CUT_MARK


def _findings_lines(findings: dict) -> list[str]:
    lines = []
    for group in findings["groups"]:
        lines.append(f"{group['title']} ({group['count']})")
        lines += [f"  {item['number']}. {cut_at_a_word(item['statement'], FINDING_LIMIT)}"
                  for item in group["items"]]
    not_rated = findings["not_rated"]
    if not_rated:
        count = f" ({not_rated['count']})" if not_rated["count"] else ""
        lines.append(f"{not_rated['title']}{count}")
        if not_rated["note"]:
            lines.append(f"  {not_rated['note']}")
    if findings["empty_message"]:
        lines.append(findings["empty_message"])
    return lines


def _choice_lines(task_id: str, status: dict, offered: list[Choice]) -> list[str]:
    lines = ["", "Choices:"]
    for number, choice in enumerate(offered, 1):
        lines += [f"{number}. {choice.name}", f"   {choice.words}"]
        if choice.name == "move-to":
            lines.append(f"   Phases: {', '.join(choice.targets)}")
    gate = status["gate"]
    if gate["kind"] == GATE_DEPLOY_FLAG_MISSING:
        guess = "yes" if status["task"]["deploy_intent"] else "no"
        lines.append(f"Moving on keeps the start-time guess: deploy={guess}.")
    lines += ["", CommandLine(f"{_CLI_CMD} decide {task_id} <choice> "
                              f"--step {status['latest_step']['step']} --gate {gate['kind']}")]
    return lines


def _headline(headline: str, latest: dict | None, reports) -> str:
    """The gate's headline. A role's own summary line is labelled as the
    role's, so it never reads as Relay's words or as a command Relay gives."""
    read = reports.get(latest["artifact"]) if latest else None
    summary = read and read["report"] and read["report"]["summary_line"]
    quoted = printable(headline, QUOTED_LIMIT)
    if summary and summary == headline:
        return f"{board_view.role_title(latest['phase'])}'s summary: {quoted}"
    return quoted


def card_lines(task_id: str, config: dict, now: datetime) -> list[str]:
    """One task's gate card, as a code block (as_code_block). A task not
    waiting on the owner gets one sentence about where it is, and no choices."""
    _check_task_id(task_id)
    status = read_api.status_payload(task_id, config)
    reports = board_view._Reports(task_id)
    view = board_view.gate_view(status, now, reports)
    latest = status["latest_step"]
    lines = [task_id]
    if latest:
        lines.append(f"Step {latest['step']}, {board_view.role_title(latest['phase'])}")
    lines.append("")
    if not view["waiting"]:
        return as_code_block(wrapped(lines + [view["headline"]]))
    lines += [board_view.row_line(row_of(status), now), _headline(view["headline"], latest, reports)]
    if view["capped"]:
        lines.append(f"{view['capped']['label']}: {view['capped']['sentence']}")
    lines += _findings_lines(view["findings"]) + _full_report_lines(task_id, latest)
    lines += ["", view["ask"]]
    return as_code_block(wrapped(lines + _choice_lines(task_id, status, choices(status))))


def _full_report_lines(task_id: str, latest: dict | None) -> list[str]:
    """Where the gate's whole report is: the `relay show` command for it."""
    if not latest or not latest.get("artifact"):
        return []
    name = printable(Path(latest["artifact"]).stem)
    return [FULL_REPORT, CommandLine(f"{_CLI_CMD} show {task_id} {name}")]


# ---------------------------------------------------------------------------
# Reports by name
# ---------------------------------------------------------------------------

def _check_task_id(task_id: str) -> None:
    """A task id reaches the file system as a folder name."""
    if not isinstance(task_id, str) or not board_view.TASK_ID.fullmatch(task_id):
        raise TaskNotFound(f"No run state for {printable(task_id)}.")


def _file_name(name: str) -> str:
    """`brief` or `03-builder`, with or without `.md`."""
    return name if name.endswith(".md") else f"{name}.md"


def _report_names(task_dir: Path) -> list[str]:
    try:
        names = sorted(entry.name for entry in task_dir.iterdir() if _is_agent_artifact(entry))
    except OSError:
        names = []
    return [BRIEF] + [printable(name) for name in names]


def without_control_characters(text: str) -> str:
    """Text a terminal shows as written: no escape, bell or carriage return.
    Line breaks and tabs are kept."""
    return "".join(character for character in text
                   if character in "\n\t" or unicodedata.category(character) != "Cc")


def report_text(task_id: str, name: str, config: dict) -> tuple[str | None, str | None]:
    """A report or the brief, whole, as (text, problem). The name is
    looked up only among the task folder's own reports; a link, a file
    over 1 MB or any other name is refused, and no path is ever given."""
    _check_task_id(task_id)
    if not load_run_state(task_id, config):
        raise TaskNotFound(f"No run state for {task_id}.")
    task_dir = settings.TASKS_DIR / task_id
    file_name = _file_name(name)
    reader = board_view.read_task_file if file_name == BRIEF else board_view._read_report_file
    text, too_large = reader(task_dir, file_name)
    if too_large:
        return None, f"{printable(file_name)} is larger than 1 MB, so it is not printed."
    if text is None:
        known = ", ".join(_report_names(task_dir))
        return None, f"No report named {printable(name)} on {task_id}. Reports: {known}."
    return without_control_characters(text), None
