"""The gate card: one task's gate as short lines a phone shows without
scrolling sideways, each choice named in words the owner can say back.

`relay waiting`, `relay card`, `relay show` and `relay decide` print what
this module gives. Every fact comes from the read API and the board's own
wording (board_view), so the card can never answer differently from the
board or `relay status --json`. It renders only and writes nothing.

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
from printable import QUOTED_LIMIT, printable
from read_api import GATE_TIME_LIMIT, GATE_WAITING_ON_OWNER, TaskNotFound
from routing import (
    GATE_ARBITER_ESCALATION, GATE_ARBITER_UNREADABLE, GATE_DEPLOY_APPROVAL, GATE_DEPLOY_FLAG_MISSING,
    GATE_RETRY_LIMIT, send_back_target,
)
from run_state import _is_agent_artifact, load_run_state
from settings import ARBITER, PIPELINE_ORDER, _CLI_CMD

# An iPhone at 390 points shows about this many characters of the Claude
# app's text on a line. A command line is the one kind allowed to be longer.
CARD_WIDTH = 44
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
START_AGAIN_WORDS = "Start {role} again from the beginning. Recorded as an override."
A_PHASE_YOU_NAME = "a phase you name (--to)"
ACCEPT_AND_LOG_WORDS = ("Accept, record the {count} remaining findings as follow-ups in the "
                        "ledger, and {then}. Recorded as an approval of this step.")
ASK_ARBITER_AGAIN_WORDS = "Run the Arbiter again from the start. Recorded as an override."
# An escalation's choice: the Arbiter's words, then what Relay runs, in Relay's.
ARBITER_OPTION_WORDS = "The Arbiter's option: {says} {runs}"
ARBITER_OPTION_RUNS = {"send-back": "Runs send-back to Builder.", "abort": "Aborts the task.",
                       "note": "Adds your note.", "move-to": "Moves to {phase} by override."}
RECOMMENDED_MARK = " (recommended)"
ARBITER_RECOMMENDS = "The Arbiter recommends: {name}"
# The Arbiter's "accept" is this choice where offered, else the plain approve or close.
ACCEPTS = ("accept-and-log", "approve", "accept-close")

# What a choice runs: the CLI command of the same name.
OVERRIDE, SEND_BACK, ABORT, NOTE = "override", "send_back", "abort", "note"

BRIEF = "brief.md"


@dataclass(frozen=True)
class Choice:
    """One decision on a card. `targets` are the phases an override may
    move to: one for approve and the closes, every phase for move-to.
    `logs` are the findings accept-and-log records as follow-ups before
    its approval, read from the status the card was made from."""
    name: str
    words: str
    action: str
    targets: tuple[str, ...] = ()
    logs: tuple[dict, ...] = ()


def items_to_log(status: dict, reports=None) -> list[dict]:
    """What accept-and-log records at this gate: at an Arbiter step, its
    follow-ups and known flaws; at a Reviewer, Tester or Security step,
    every rated finding the board reads from its report. Each is
    {kind, text, severity, from}."""
    latest = status["latest_step"] or {}
    ruling = status["gate"].get("arbiter")
    if latest.get("phase") == ARBITER:
        if not ruling:
            return []
        return ([{"kind": "follow_up", "text": text, "severity": None, "from": latest.get("artifact")}
                 for text in ruling.get("follow_ups") or []]
                + [{"kind": "known_flaw", "text": text, "severity": None, "from": latest.get("artifact")}
                   for text in ruling.get("known_flaws") or []])
    if latest.get("phase") not in board_view._ROLES_WITH_FINDINGS or not latest.get("artifact"):
        return []
    reports = reports or board_view._Reports(status["task"]["task_id"])
    read = reports.get(latest["artifact"])
    if not read or read["too_large"] or not read["report"]:
        return []
    return [{"kind": "finding", "text": finding["statement"], "severity": finding["severity"],
             "from": latest["artifact"]} for finding in read["report"]["findings"]]


def _arbiter_options(gate: dict) -> tuple[list[Choice], list[Choice]]:
    """An escalation's choices, each in the Arbiter's words and then in
    Relay's for what it runs: those that neither close the task nor reach
    Ops, and those that do, which the card lists after every other choice
    unless the Arbiter recommends one (routing never lets it while Security
    holds a high or the latest run failed)."""
    ruling = gate.get("arbiter") or {}
    staying, leaving = [], []
    for listed in ruling.get("choices") or []:
        runs = ARBITER_OPTION_RUNS[listed["does"]].format(phase=board_view.role_title(listed["phase"]))
        words = ARBITER_OPTION_WORDS.format(says=printable(listed["says"], QUOTED_LIMIT), runs=runs)
        action, targets = {"send-back": (SEND_BACK, ("builder",)), "abort": (ABORT, ()),
                           "note": (NOTE, ()),
                           "move-to": (OVERRIDE, (listed["phase"],))}[listed["does"]]
        leaves = listed["does"] == "move-to" and listed["phase"] in ("done", "ops")
        (leaving if leaves else staying).append(Choice(listed["name"], words, action, targets))
    return staying, leaving


def _approvals(status: dict, kind: str, next_phase, reports) -> list[Choice]:
    """approve, accept-close or deploy-done, then accept-and-log where
    there are findings to log."""
    if kind == GATE_DEPLOY_APPROVAL:
        return [Choice("deploy-done", DEPLOY_DONE_WORDS, OVERRIDE, ("done",))]
    if next_phase == "done":
        approve = Choice("accept-close", ACCEPT_CLOSE_WORDS, OVERRIDE, ("done",))
        then = "close the task"
    else:
        approve = Choice("approve", APPROVE_WORDS.format(next=board_view.role_title(next_phase)),
                         OVERRIDE, (next_phase,))
        then = f"move on to {board_view.role_title(next_phase)}"
    logged = tuple(items_to_log(status, reports))
    if not logged:
        return [approve]
    return [approve, Choice("accept-and-log", ACCEPT_AND_LOG_WORDS.format(count=len(logged), then=then),
                            OVERRIDE, approve.targets, logs=logged)]


def choices(status: dict, reports=None) -> list[Choice]:
    """The choices at the task's gate, in the order the card shows them,
    from the gate the read API gives. None when nothing waits on the owner.
    The Arbiter's recommended choice, where it gave one, comes first."""
    gate = status["gate"]
    if gate["state"] != GATE_WAITING_ON_OWNER:
        return []
    kind, next_phase = gate["kind"], gate["next_phase"]
    offered, leaving = [], []
    if kind == GATE_TIME_LIMIT:
        offered.append(Choice("start-again",
                              START_AGAIN_WORDS.format(role=board_view.role_title(next_phase)),
                              OVERRIDE, (next_phase,)))
    if kind == GATE_ARBITER_ESCALATION:
        staying, leaving = _arbiter_options(gate)
        offered += staying
    if gate["approve_command"]:
        offered += _approvals(status, kind, next_phase, reports)
    if gate["send_back_command"]:
        role = next_phase if kind == GATE_RETRY_LIMIT else send_back_target(status["latest_step"]["phase"])
        offered.append(Choice("send-back", SEND_BACK_WORDS.format(role=board_view.role_title(role)),
                              SEND_BACK, (role,)))
    if kind == GATE_ARBITER_UNREADABLE:
        offered.append(Choice("ask-arbiter-again", ASK_ARBITER_AGAIN_WORDS, OVERRIDE, (ARBITER,)))
    # One way to close on a card: move-to leaves out `done` where a close is offered.
    closes = any(choice.targets == ("done",) for choice in offered + leaving)
    phases = tuple(PIPELINE_ORDER) + (() if closes else ("done",))
    offered += [Choice("move-to", MOVE_TO_WORDS.format(phase=A_PHASE_YOU_NAME), OVERRIDE, phases),
                Choice("abort", ABORT_WORDS, ABORT),
                Choice("note", NOTE_WORDS, NOTE)]
    offered += leaving
    recommended = recommended_choice(status, offered)
    return ([choice for choice in offered if choice.name == recommended]
            + [choice for choice in offered if choice.name != recommended])


def recommended_choice(status: dict, offered: list[Choice]) -> str | None:
    """The name of the choice the Arbiter recommends here, or None. Only
    the Arbiter's recommendation is ever marked: "accept" is accept-and-log
    where offered, else approve or accept-close."""
    recommends = (status["gate"].get("arbiter") or {}).get("recommends")
    names = [choice.name for choice in offered]
    if recommends == "accept":
        return next((name for name in ACCEPTS if name in names), None)
    return recommends if recommends in names else None


def decided_words(choice: Choice, to_phase: str | None) -> str:
    """A choice's words once made: move-to names the phase it moved to."""
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


class RoleTextLine(str):
    """A labelled line of a role's own words. Its continuation lines are
    indented, so no wrap of its text can start a line the way Relay's own
    lines start ("Relay warns: ...")."""


ROLE_TEXT_CONTINUATION_INDENT = "  "


def wrapped(lines: list[str]) -> list[str]:
    """Every line cut to CARD_WIDTH at spaces, keeping an indent. A
    CommandLine is kept whole, so it can be copied. A RoleTextLine's
    continuation lines are indented further."""
    out = []
    for line in lines:
        if isinstance(line, CommandLine) or not line.strip():
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        continuation = indent + (ROLE_TEXT_CONTINUATION_INDENT if isinstance(line, RoleTextLine) else "")
        out += textwrap.wrap(printable(line, SENTENCE_LIMIT), width=CARD_WIDTH,
                             initial_indent=indent, subsequent_indent=continuation,
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
            "time_limit": gate.get("time_limit"), "latest_step": status["latest_step"]}


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


def _findings_lines(findings: dict) -> list[str]:
    lines = []
    for group in findings["groups"]:
        lines.append(f"{group['title']} ({group['count']})")
        lines += [f"  {item['number']}. {printable(item['statement'], QUOTED_LIMIT)}"
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
    recommended = recommended_choice(status, offered)
    for number, choice in enumerate(offered, 1):
        mark = RECOMMENDED_MARK if choice.name == recommended else ""
        lines += [f"{number}. {choice.name}{mark}", f"   {choice.words}"]
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
    if not view["waiting"]:
        return as_code_block(wrapped(lines + ["", view["headline"]]))
    offered = choices(status, reports)
    recommended = recommended_choice(status, offered)
    if view["relay_warning"]:
        lines.append(view["relay_warning"])
    if recommended:
        lines.append(ARBITER_RECOMMENDS.format(name=recommended))
    lines += [RoleTextLine(line) for line in view["arbiter_lines"]]
    lines.append("")
    # A report left by a role the time limit stopped is never read, not even for its summary.
    headline_step = None if status["gate"]["kind"] == GATE_TIME_LIMIT else latest
    lines += [board_view.row_line(row_of(status), now),
              _headline(view["headline"], headline_step, reports)]
    lines += [RoleTextLine(printable(line, QUOTED_LIMIT)) for line in view.get("sizing_lines", [])]
    if view["capped"]:
        lines.append(f"{view['capped']['label']}: {view['capped']['sentence']}")
    lines += _findings_lines(view["findings"]) + ["", view["ask"]]
    return as_code_block(wrapped(lines + _choice_lines(task_id, status, offered)))


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
