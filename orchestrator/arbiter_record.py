"""What Relay hands the Arbiter, the words it hands on after a ruling, and
the rulings and accepted follow-ups read back from the ledger.

Every string an Arbiter wrote is untrusted role text. It reaches a prompt
only inside a block labelled as the Arbiter's words, cut by printable, and
never as Relay's or the owner's. The handover is built from file names,
ledger fields and the Arbiter's own earlier reasons: it opens no report.
"""

import arbiter
import db
import evidence
import settings
from owner_notes import read_owner_notes
from printable import printable
from recorded_runs import read_test_runs
from routing import VERDICTS_RELAY_TESTS
from settings import _sqlite_enabled, get_db_connection

SOURCE_ARBITER, SOURCE_OWNER = "arbiter", "owner"
KIND_FOLLOW_UP, KIND_KNOWN_FLAW = "follow_up", "known_flaw"
KIND_FINDING = "finding"

DECISION_WORDS = {arbiter.MOVE_ON: "move on", arbiter.TARGETED_ROUND: "targeted round",
                  arbiter.REPLAN: "re-plan", arbiter.ESCALATE: "escalate"}

ACCEPTED_HEADING = ("Accepted and logged on this task, so do not raise any of these again unless "
                    "you name a consequence that was not weighed. Each is quoted as its source "
                    "wrote it, not as Relay's words or the owner's instruction:")
# The Arbiter's follow-ups and known flaws from a move-on the owner did not
# approve: recorded, never accepted, and never a reason to stay quiet.
ARBITER_RECORDED_HEADING = ("Recorded by the Arbiter on this task, not accepted by the owner. They "
                            "waive no check your role requires: raise any of them again if it "
                            "still matters. Each is quoted as the Arbiter wrote it, not as Relay's "
                            "words or the owner's instruction:")
ONE_ROUND_ONLY = ("These are the Arbiter's words for this round only. They are not the owner's, "
                  "and they do not waive a check your role requires.")


def _payload(event: dict) -> dict:
    """An event's payload, or {} when a hand-edited ledger left it unreadable."""
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _text_list(value) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def ruling_record(ruling: "arbiter.Ruling") -> dict:
    """The checked fields of a ruling, as the ledger keeps them."""
    return {"decision": ruling.decision, "reason": ruling.reason, "replan_to": ruling.replan_to,
            "fix": list(ruling.fix), "do_not_reopen": list(ruling.do_not_reopen),
            "follow_ups": list(ruling.follow_ups), "known_flaws": list(ruling.known_flaws),
            "question": ruling.question,
            "choices": [{"name": choice.name, "says": choice.says, "does": choice.action,
                         "phase": choice.phase} for choice in ruling.choices],
            "recommended": ruling.recommended}


# ---------------------------------------------------------------------------
# Reading rulings and follow-ups back
# ---------------------------------------------------------------------------

def _arbiter_events(task_id: str, config: dict) -> list[dict]:
    if not _sqlite_enabled(config):
        return []
    types = (db.EVENT_ARBITER_STARTED, db.EVENT_ARBITER_RULED, db.EVENT_ARBITER_UNREADABLE,
             db.EVENT_FOLLOW_UPS_LOGGED)
    return [event for event in db.get_events(get_db_connection(), task_id)
            if event["event_type"] in types]


def read_rulings(task_id: str, config: dict) -> list[dict]:
    """Every Arbiter ruling Relay acted on, and every report it could not,
    oldest first. `decision` is None for an unreadable one. The files
    backend keeps no events, so it has none."""
    events = _arbiter_events(task_id, config)
    started = {event["step"]: _payload(event) for event in events
               if event["event_type"] == db.EVENT_ARBITER_STARTED}
    rulings = []
    for event in events:
        if event["event_type"] not in (db.EVENT_ARBITER_RULED, db.EVENT_ARBITER_UNREADABLE):
            continue
        payload, began = _payload(event), started.get(event["step"], {})
        ruling = payload.get("ruling") if isinstance(payload.get("ruling"), dict) else {}
        decision = ruling.get("decision")
        trigger = began.get("trigger")
        rulings.append({"step": event["step"], "report": _text_or_none(payload.get("report")),
                        "situation": _text_or_none(payload.get("situation") or began.get("situation")),
                        "decision": decision if decision in arbiter.DECISIONS else None,
                        "reason": _text_or_none(ruling.get("reason")),
                        "replan_to": (ruling.get("replan_to")
                                      if ruling.get("replan_to") in arbiter.REPLAN_TARGETS else None),
                        "routed": payload.get("routed") is True,
                        "trigger": trigger if isinstance(trigger, dict) else None,
                        "problem": _text_or_none(payload.get("problem")),
                        "timestamp": event["timestamp"]})
    return rulings


def recorded_report(task_id: str, step, config: dict) -> dict | None:
    """The ruling Relay recorded for the Arbiter's `step`, in the shape of
    the frontmatter it was read from. Once Relay has acted on a ruling,
    routing and the card read this and not the report, as every other role
    is routed on the verdict Relay recorded: a report changed afterwards
    changes nothing. None when no ruling was recorded for the step (not
    yet, an unreadable report, or the files backend, which keeps no
    events); the report is read then."""
    for event in _arbiter_events(task_id, config):
        if event["event_type"] != db.EVENT_ARBITER_RULED or event["step"] != step:
            continue
        ruling = _payload(event).get("ruling")
        if isinstance(ruling, dict) and ruling.get("decision") in arbiter.DECISIONS:
            return _as_frontmatter(ruling)
    return None


def _as_frontmatter(ruling: dict) -> dict:
    """A recorded ruling (ruling_record) turned back into frontmatter, for
    arbiter.read_ruling to check again: a hand-edited ledger row that does
    not fit is refused, never routed."""
    frontmatter = {field: value for field, value in ruling.items()
                   if field not in ("decision", "choices", "recommended")}
    frontmatter["verdict"] = ruling["decision"]
    choices = ruling.get("choices")
    if not isinstance(choices, list) or not all(isinstance(choice, dict) for choice in choices):
        frontmatter["choices"] = choices
        return frontmatter
    frontmatter["choices"] = [{"says": choice.get("says"), "does": _choice_does(choice)}
                              for choice in choices]
    names = [choice.get("name") for choice in choices]
    if ruling.get("recommended") in names:
        frontmatter["recommended"] = names.index(ruling["recommended"]) + 1
    return frontmatter


def _choice_does(choice: dict):
    """A recorded choice's action and phase as the report's `does` said
    them (`move-to tester`, or the action alone)."""
    action, phase = choice.get("does"), choice.get("phase")
    return action if phase is None else f"{action} {phase}"


def _text_or_none(value) -> str | None:
    """A ledger field that should be text; anything else (a hand-edited
    row) reads as not recorded."""
    return value if isinstance(value, str) else None


def read_follow_ups(task_id: str, config: dict) -> list[dict]:
    """Every item accepted and logged on the task, oldest first: the
    follow-ups and known flaws of each ruling Relay moved the task on, and
    each finding the owner accepted and logged at a gate."""
    items = []
    for event in _arbiter_events(task_id, config):
        payload = _payload(event)
        base = {"step": event["step"], "report": payload.get("report"),
                "timestamp": event["timestamp"]}
        if event["event_type"] == db.EVENT_ARBITER_RULED and payload.get("routed") is True:
            ruling = payload.get("ruling") if isinstance(payload.get("ruling"), dict) else {}
            for field, kind in (("follow_ups", KIND_FOLLOW_UP), ("known_flaws", KIND_KNOWN_FLAW)):
                items += [{**base, "source": SOURCE_ARBITER, "kind": kind, "text": text,
                           "severity": None} for text in _text_list(ruling.get(field))]
        elif event["event_type"] == db.EVENT_FOLLOW_UPS_LOGGED:
            logged = payload.get("items")
            for item in logged if isinstance(logged, list) else []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    items.append({**base, "source": SOURCE_OWNER, "kind": item.get("kind"),
                                  "text": item["text"], "severity": item.get("severity"),
                                  "report": item.get("from") or base["report"]})
    return items


# ---------------------------------------------------------------------------
# Words handed to later roles
# ---------------------------------------------------------------------------

def _item_line(item: dict) -> str:
    text = printable(item["text"], arbiter.MOST_ITEM_CHARACTERS)
    if item["source"] == SOURCE_ARBITER:
        what = "a known flaw" if item["kind"] == KIND_KNOWN_FLAW else "a follow-up"
        return f"- (the Arbiter, step {item['step']}, {what}) {text}"
    details = [f"accepted by the owner at step {item['step']}"]
    if item.get("report"):
        details.append(f"from {printable(item['report'])}")
    if item.get("severity"):
        details.append(printable(item["severity"]))
    return f"- ({', '.join(details)}) {text}"


def accepted_items_prompt(task_id: str, config: dict) -> str:
    """What every later role is given: what the owner accepted and logged
    on the task, under the heading that asks not to raise it again, and
    apart from it what the Arbiter alone recorded, which no role is ever
    told to leave unraised. Empty when there is nothing."""
    items = read_follow_ups(task_id, config)
    owners = [item for item in items if item["source"] == SOURCE_OWNER]
    arbiters = [item for item in items if item["source"] == SOURCE_ARBITER]
    blocks = [[heading] + [_item_line(item) for item in group]
              for heading, group in ((ACCEPTED_HEADING, owners), (ARBITER_RECORDED_HEADING, arbiters))
              if group]
    return "".join("\n\n" + "\n".join(block) for block in blocks)


def _quoted(lines) -> list[str]:
    return [f"- {printable(line, arbiter.MOST_ITEM_CHARACTERS)}" for line in lines]


def targeted_round_words(ruling: "arbiter.Ruling", report_path) -> str:
    """What the Builder of a targeted round is told: the Arbiter's fix and
    do-not-reopen lists, as its words, for that round only."""
    lines = [f"The Arbiter ({report_path}) sent this round back. In the Arbiter's words, fix:"]
    lines += _quoted(ruling.fix)
    lines.append("Do not reopen:")
    lines += _quoted(ruling.do_not_reopen) or ["(none named)"]
    lines.append(ONE_ROUND_ONLY)
    return "\n\n" + "\n".join(lines)


def replan_words(ruling: "arbiter.Ruling", report_path) -> str:
    """What the Architect or Designer of a re-plan is told: why the Arbiter
    found the plan or the brief to be the problem, in its words."""
    what = "brief" if ruling.replan_to == "designer" else "plan"
    return (f"\n\nThe Arbiter ({report_path}) ruled that the {what} is the problem. Its reason, "
            f"in its words: {printable(ruling.reason, arbiter.MOST_REASON_CHARACTERS)}")


# ---------------------------------------------------------------------------
# What Relay hands the Arbiter
# ---------------------------------------------------------------------------

SITUATION_WORDS = {
    arbiter.SITUATION_LOOP: "a loop: a report still holds a high after the review loops that "
                            "call you on this plan",
    arbiter.SITUATION_WARNING: "a warning gate: a report passed with something still wrong",
}


NOT_RECORDED = "a commit not recorded"


def _path(task_id: str, name) -> str:
    return str(settings.TASKS_DIR / task_id / name) if name else "(no report recorded)"


def _situation_section(task_id: str, trigger: "arbiter.Trigger", artifacts: dict) -> list[str]:
    calls = ", ".join(arbiter.CALLS_OPEN[trigger.situation])
    return ["## The situation",
            f"You are called at {SITUATION_WORDS[trigger.situation]}.",
            f"The calls open here: {calls}.",
            f"The report you rule on: step {trigger.step}, {trigger.phase} ({trigger.verdict}): "
            f"{_path(task_id, artifacts.get(trigger.step))}"]


def _count_lines(label: str, counts: dict) -> list[str]:
    by_role = ", ".join(f"{role} {count}" for role, count in counts["review_loop_by_role"].items())
    by_starter = ", ".join(f"{starter} {count}"
                           for starter, count in counts["review_loop_by_starter"].items())
    return [f"{label}: {counts['total']} Builder steps.",
            f"  first build {counts[arbiter.CAUSE_FIRST_BUILD]}; review loops "
            f"{counts[arbiter.CAUSE_REVIEW_LOOP]} (by role: {by_role}; started by: {by_starter}); "
            f"failed test runs {counts[arbiter.CAUSE_FAILED_TEST_RUN]}; after a re-plan "
            f"{counts[arbiter.CAUSE_AFTER_REPLAN]}; owner's override from elsewhere "
            f"{counts[arbiter.CAUSE_OWNER_ELSEWHERE]}."]


def _counts_section(counts: "arbiter.LoopCounts") -> list[str]:
    replan = (f"The latest re-plan is step {counts.replan_step}." if counts.replan_step is not None
              else "There has been no re-plan since the first build.")
    return (["## The counts", replan]
            + _count_lines("For the task", counts.by_cause(False))
            + _count_lines("Since the latest re-plan", counts.by_cause(True)))


def _plans_section(task_id: str, entries: list[dict], artifacts: dict) -> list[str]:
    lines = ["## The brief and the plans", f"- The task brief: {settings.TASKS_DIR / task_id / 'brief.md'}"]
    architects = [entry["step"] for entry in entries if entry.get("phase") == "architect"]
    for entry in entries:
        if entry.get("phase") == "designer":
            lines.append(f"- Step {entry['step']}, a Designer report: {_path(task_id, artifacts.get(entry['step']))}")
        elif entry.get("phase") == "architect":
            label = "the current plan" if entry["step"] == architects[-1] else "an earlier plan"
            lines.append(f"- Step {entry['step']}, {label}: {_path(task_id, artifacts.get(entry['step']))}")
    return lines


def _ruling_reasons(task_id: str, config: dict) -> dict:
    return {ruling["step"]: ruling for ruling in read_rulings(task_id, config)}


def _step_lines(task_id: str, state: dict, entries: list[dict], artifacts: dict, test_runs: list,
                rulings: dict) -> list[tuple]:
    """(step, rank, timestamp, line) for each report, round and earlier
    Arbiter step."""
    items, builds = [], 0
    base = state.get("base")
    start = f"the base ({printable(base)})" if base else NOT_RECORDED
    runs_by_step = {}
    for run in test_runs:
        runs_by_step.setdefault(run.get("step"), []).append(run)
    for entry in entries:
        step, phase, verdict = entry["step"], entry.get("phase"), entry.get("verdict")
        when = str(entry.get("timestamp") or "")
        if phase == "builder":
            commit = next((run.get("commit") for run in reversed(runs_by_step.get(step, []))
                           if run.get("commit")), None)
            end = f"commit {printable(commit)[:12]}" if commit else NOT_RECORDED
            items.append((step, 0, when, f"- Step {step}, round {builds} starts at {start}, "
                                         f"ends at {end}: {_path(task_id, artifacts.get(step))}"))
            start, builds = end, builds + 1
        elif phase in arbiter.RATING_PHASES:
            items.append((step, 0, when, f"- Step {step}, round {max(builds - 1, 0)}, {phase} "
                                         f"({verdict or 'no verdict'}): {_path(task_id, artifacts.get(step))}"))
        elif phase == settings.ARBITER:
            ruled = rulings.get(step) or {}
            decision = DECISION_WORDS.get(ruled.get("decision")) or verdict or "no ruling recorded"
            situation = ruled.get("situation") or "situation not recorded"
            line = f"- Step {step}, the Arbiter ({decision}, {situation}): {_path(task_id, artifacts.get(step))}"
            if ruled.get("reason"):
                line += (f"\n    Its reason, in the Arbiter's words: "
                         f"{printable(ruled['reason'], arbiter.MOST_REASON_CHARACTERS)}")
            items.append((step, 0, when, line))
    return items


def _note_step(note: dict, entries: list[dict]) -> int:
    """A note's step: its own (a `relay note`), or else the latest step
    dispatched by the note's time (a send-back or an approval, which the
    ledger keeps without a step). Times are to the second, so steps
    dispatched in the same second as the note count as before it."""
    if isinstance(note.get("step"), int):
        return note["step"]
    noted = str(note.get("timestamp") or "")
    dispatched = [entry["step"] for entry in entries if str(entry.get("timestamp") or "") <= noted]
    return dispatched[-1] if dispatched else 0


def _in_step_order(task_id: str, state: dict, current_step: int, config: dict,
                   artifacts: dict) -> list[str]:
    entries = [entry for entry in arbiter.steps_in_order(state.get("history")) if entry["step"] < current_step]
    test_runs = read_test_runs(task_id, config)
    items = _step_lines(task_id, state, entries, artifacts, test_runs, _ruling_reasons(task_id, config))
    items += [(run.get("step") or 0, 1, str(run.get("timestamp") or ""),
               f"- After step {run.get('step')}, Relay's test run: {evidence.summary(run)}")
              for run in test_runs]
    for note in read_owner_notes(task_id, config):
        lines = [f"- The owner ({note['kind']}, at {note['phase']}, {note['timestamp']}):"]
        lines += [f"    {line}" for line in note["note"].splitlines()]
        items.append((_note_step(note, entries), 2, note["timestamp"], "\n".join(lines)))
    items.sort(key=lambda item: (item[0], item[1], item[2]))
    return ["## In step order"] + [item[3] for item in items]


def _contract_section() -> list[str]:
    return [
        "## What Relay reads from your report",
        "Relay reads only your report's frontmatter, never its prose. Besides task_id, phase, "
        "step and date, it reads:",
        f"- verdict: one of {', '.join(arbiter.DECISIONS)}.",
        f"- reason: one line, at most {arbiter.MOST_REASON_CHARACTERS} characters. Always.",
        "- follow_ups and known_flaws: lists of short lines, for any call.",
        "- For targeted-round: fix (a list, not empty) and do_not_reopen (a list).",
        "- For re-plan: replan_to: architect or designer.",
        f"- For escalate: question (one line), choices (2 to {arbiter.MOST_CHOICES} items, each "
        "with says, in your words, and does: send-back, abort, note, or move-to <phase>), and "
        "recommended (the number of the choice you recommend, counting from 1).",
        f"Each list holds at most {arbiter.MOST_LIST_ITEMS} lines of at most "
        f"{arbiter.MOST_ITEM_CHARACTERS} characters; each choice's says at most "
        f"{arbiter.MOST_CHOICE_CHARACTERS}. A report Relay cannot act on exactly stops for "
        "the owner."]


def handover_prompt(task_id: str, state: dict, step: int, config: dict, artifacts: dict) -> str:
    """What Relay hands the Arbiter at `step`, after its usual prompt: the
    situation and the calls open in it, the counts by cause, the brief and
    every plan, and every report, round, test run, owner's note and earlier
    ruling in step order, then what Relay reads from its report.
    `artifacts` is each step's recorded file name (read_api._step_artifacts)."""
    trigger = arbiter.trigger_of(state.get("history"), step)
    if trigger is None:
        return ""
    entries = [entry for entry in arbiter.steps_in_order(state.get("history")) if entry["step"] < step]
    counts = arbiter.loop_counts(state.get("history"), read_test_runs(task_id, config),
                                 VERDICTS_RELAY_TESTS)
    sections = [_situation_section(task_id, trigger, artifacts), _counts_section(counts),
                _plans_section(task_id, entries, artifacts),
                _in_step_order(task_id, state, step, config, artifacts), _contract_section()]
    return "\n\n" + "\n\n".join("\n".join(section) for section in sections)
