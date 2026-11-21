"""How big a task is and whether it is risky, as its brief says.

Relay sizes the chain to the task instead of running all seven roles on
everything:

- size `fix`: a one-line definition of fixed; no Designer, no Architect.
  The task starts at the Builder.
- size `feature`: the Designer writes the brief; the Architect runs only
  when the Designer says the structure changes (`ready-for-architect`).
- size `project`: the Designer, then always the Architect.
- `risky: yes` (auth, input, money, production data): the Tester and
  Security run after the Reviewer. `risky: no`: the Reviewer is the last
  check before Ops (deploy tasks) or done.

All three come from the frontmatter of the task's `brief.md`, which the
owner (or the session acting for the owner) writes before `relay start`: `size`,
`risky`, and, for a fix or a feature (which may never reach the Architect,
whose plan otherwise decides it), `deploy: yes|no`. A brief that leaves any
of them out, says something else, or cannot be read gets the full chain:
size `project`, risky. Nothing here is ever guessed
toward less checking.

`relay start` records what it read in the ledger (a `sized` event). A brief
edited mid-run (by a role, say) changes nothing. The sizing can then be
raised, never lowered, by the Reviewer or the Arbiter in its report's
frontmatter (judgment_in), and changed either way by the owner with
`relay resize`. Each change is a further `sized` event, and every read
takes the latest, so a Relay rolled back to before raises existed still
routes on the raised sizing. On the files backend, which keeps no events,
the raises are read back from the Reviewer's and the Arbiter's reports.
"""

import codecs
import re
from dataclasses import dataclass, replace

import json

import bounded_yaml
import db
import settings
from printable import printable

SIZES = ("fix", "feature", "project")
FULL_CHAIN_SIZE = "project"
# How far up the chain each size goes: a raise moves only up this order.
SIZE_ORDER = {"fix": 0, "feature": 1, "project": 2}
# The roles whose report may raise the sizing, and the answers it may give.
JUDGING_ROLES = ("reviewer", "arbiter")
ANSWERS = ("raise", "keep")
ROLE_NAMES = {"reviewer": "Reviewer", "arbiter": "Arbiter", "owner": "owner"}
# A reason a role or the owner gives for a sizing is kept to one line of this many characters.
MOST_REASON_CHARACTERS = 300
NO_REASON_GIVEN = "(the report gave no reason)"
# The most of a report the files backend reads to find its frontmatter.
REPORT_HEAD_BYTES = bounded_yaml.MOST_FRONTMATTER_CHARACTERS
_JUDGING_REPORT = re.compile(r"(\d{2})-(reviewer|arbiter)[A-Za-z0-9._-]*\.md")


@dataclass(frozen=True)
class Sizing:
    size: str
    risky: bool
    # Why the full chain was used in place of what the brief says, or None
    # when the brief said all it must and all of it was read.
    note: str | None = None
    # The brief's deploy: yes|no; None on the full chain, where the
    # Architect's plan decides.
    deploy: bool | None = None


FULL_CHAIN = Sizing(FULL_CHAIN_SIZE, True)


def _full_chain(why: str) -> Sizing:
    return Sizing(FULL_CHAIN_SIZE, True, f"{why}, so the full chain runs")


def yes_or_no(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "true"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("no", "false"):
        return False
    return None


def size_word(value) -> str | None:
    """A size as one of SIZES, read in any case with spaces around it
    ignored, like the yes/no words; None when it is not one."""
    if isinstance(value, str) and value.strip().lower() in SIZES:
        return value.strip().lower()
    return None


def sizing_in(text: str) -> Sizing:
    """The sizing a brief's text gives. Any field missing or not one of
    its words gives the whole full chain, not just that part of it."""
    try:
        data = bounded_yaml.frontmatter(text)
    except bounded_yaml.NotRead as problem:
        return _full_chain(f"the brief's frontmatter {problem}")
    size = size_word(data.get("size"))
    risky, deploy = yes_or_no(data.get("risky")), yes_or_no(data.get("deploy"))
    problems = []
    if size is None:
        problems.append("size: fix|feature|project" if data.get("size") is None
                        else f"size {data.get('size')!r} (not fix, feature or project)")
    if risky is None:
        problems.append("risky: yes|no" if data.get("risky") is None
                        else f"risky {data.get('risky')!r} (not yes or no)")
    if size in ("fix", "feature") and deploy is None:
        problems.append("deploy: yes|no" if data.get("deploy") is None
                        else f"deploy {data.get('deploy')!r} (not yes or no)")
    if problems:
        return _full_chain(f"the brief does not give {' or '.join(problems)}")
    return Sizing(size, risky, None, deploy)


def read_brief_sizing(task_id: str) -> Sizing:
    """The sizing the task's brief gives now; the full chain when it has none."""
    brief = settings.TASKS_DIR / task_id / "brief.md"
    try:
        text = brief.read_text()
    except (OSError, UnicodeDecodeError):
        return _full_chain("the brief could not be read")
    return sizing_in(text)


def record(task_id: str, sizing: Sizing, config: dict) -> None:
    """Record the sizing `relay start` read, as a `sized` ledger event."""
    if not settings._sqlite_enabled(config):
        return
    db.record_event(settings.get_db_connection(), task_id, db.EVENT_SIZED,
                    {"size": sizing.size, "risky": sizing.risky, "deploy": sizing.deploy,
                     "note": sizing.note})


def _payload(event: dict):
    payload = event.get("payload")
    return json.loads(payload) if isinstance(payload, str) else payload


def sizing_of_event(event: dict) -> Sizing:
    """The sizing a `sized` event records. Raises ValueError when its
    payload cannot be read as one."""
    try:
        payload = _payload(event)
        size, risky = payload["size"], payload["risky"]
        if size not in SIZES or not isinstance(risky, bool):
            raise ValueError
        deploy = payload.get("deploy")
        return Sizing(size, risky, payload.get("note"), deploy if isinstance(deploy, bool) else None)
    except (TypeError, ValueError, KeyError, AttributeError):
        raise ValueError("the recorded sizing could not be read") from None


def sized_events(task_id: str) -> list[dict]:
    """The task's `sized` events, oldest first (sqlite only)."""
    return [event for event in db.get_events(settings.get_db_connection(), task_id)
            if event["event_type"] == db.EVENT_SIZED]


def read_sizing(task_id: str, config: dict | None = None) -> Sizing:
    """The sizing a task routes on: the latest one recorded (at its start,
    or a later change) when there is one, else what its brief says (a task
    started before sizing was recorded). On the files backend, the brief's
    sizing with the raises its Reviewer and Arbiter reports gave."""
    if config is not None and settings._sqlite_enabled(config):
        events = sized_events(task_id)
        if events:
            try:
                return sizing_of_event(events[-1])
            except ValueError as problem:
                return _full_chain(str(problem))
        return read_brief_sizing(task_id)
    return _folded(read_brief_sizing(task_id), task_id)


def read_sizing_before_step(task_id: str, config: dict | None, step: int | None) -> Sizing:
    """The sizing as it stood before the report at `step` was read. Under
    sqlite that is the latest recorded one (a step's own change is looked up
    by the caller); on the files backend, the brief's sizing with the raises
    of the reports before that step only."""
    if config is not None and settings._sqlite_enabled(config):
        return read_sizing(task_id, config)
    return _folded(read_brief_sizing(task_id), task_id, before_step=step)


# ---------------------------------------------------------------------------
# Raising the sizing from a role's report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Judgment:
    """What one Reviewer's or Arbiter's report says of the task's sizing,
    as Relay read it from the report's frontmatter.

    `before` is the sizing as it stood before the report; `after` is the
    sizing routing uses for it (equal to `before` when nothing is raised).
    `answer` is "raise", "keep" or None; `reason` is the report's words, on
    one line. `refused` holds, in Relay's words, each value outside the
    allowed words, and `lowered` each lowering the report asked for: Relay
    used neither. `unanswered` is Relay's one-line problem, set only where
    an answer was required and is missing or cannot be used."""
    by: str
    step: int | None
    before: Sizing
    after: Sizing
    answer: str | None = None
    reason: str | None = None
    refused: tuple[str, ...] = ()
    lowered: tuple[str, ...] = ()
    unanswered: str | None = None

    @property
    def asked_to_lower(self) -> bool:
        return bool(self.lowered)

    @property
    def changed(self) -> bool:
        return (self.after.size, self.after.risky) != (self.before.size, self.before.risky)

    @property
    def raised_to_project(self) -> bool:
        return self.before.size != "project" and self.after.size == "project"


def one_line_reason(value) -> str | None:
    """A reason as one line of at most MOST_REASON_CHARACTERS characters,
    with nothing a terminal would act on; None when it is not text or is
    blank."""
    if not isinstance(value, str):
        return None
    return printable(value, MOST_REASON_CHARACTERS).strip() or None


def _read_answer(frontmatter: dict) -> tuple[str | None, str | None]:
    """(the answer, the refusal of an answer that is not one of ANSWERS)."""
    if "sizing" not in frontmatter or frontmatter["sizing"] is None:
        return None, None
    value = frontmatter["sizing"]
    if isinstance(value, str) and value.strip().lower() in ANSWERS:
        return value.strip().lower(), None
    return None, f"sizing {printable(value, 40)!r} (not raise or keep)"


def _raised_size(value, before: str) -> tuple[str, str | None, bool]:
    """(the size after, a refusal, whether it asked to lower) for a report's `size:`."""
    size = size_word(value)
    if size is None:
        return before, f"size {printable(value, 40)!r} (not fix, feature or project)", False
    if SIZE_ORDER[size] < SIZE_ORDER[before]:
        return before, f"size: {size} (lower than {before})", True
    return max(size, before, key=SIZE_ORDER.__getitem__), None, False


def _raised_risky(value, before: bool) -> tuple[bool, str | None, bool]:
    """(risky after, a refusal, whether it asked to lower) for a report's `risky:`."""
    risky = yes_or_no(value)
    if risky is None:
        return before, f"risky {printable(value, 40)!r} (not yes or no)", False
    if before and not risky:
        return before, "risky: no (lower than yes)", True
    return before or risky, None, False


def judgment_in(frontmatter: dict, before: Sizing, *, by: str, step: int | None,
                answer_required: bool) -> Judgment:
    """What a report's frontmatter says of the sizing. Pure.

    `size:` and `risky:` are read only under `sizing: raise`; each is
    raised when it is higher than `before`, left alone when it is the same,
    and refused when it is lower or not one of its words. So a report that
    raises one field and lowers the other gets the raise only. A raise with
    no reason still applies: refusing it would mean less checking. The
    deploy flag is never changed."""
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    answer, answer_refused = _read_answer(frontmatter)
    reason = one_line_reason(frontmatter.get("sizing_reason"))
    size, risky, refused, lowered = before.size, before.risky, [], []
    if answer_refused:
        refused.append(answer_refused)
    if answer == "raise":
        if "size" in frontmatter:
            size, problem, is_lowering = _raised_size(frontmatter["size"], before.size)
            (lowered if is_lowering else refused).extend([problem] if problem else [])
        if "risky" in frontmatter:
            risky, problem, is_lowering = _raised_risky(frontmatter["risky"], before.risky)
            (lowered if is_lowering else refused).extend([problem] if problem else [])
    after = replace(before, size=size, risky=risky) if (size, risky) != (before.size, before.risky) \
        else before
    judgment = Judgment(by, step, before, after, answer, reason, tuple(refused), tuple(lowered))
    if judgment.changed and not reason:
        judgment = replace(judgment, reason=NO_REASON_GIVEN)
    if answer_required:
        judgment = replace(judgment, unanswered=_unanswered(judgment, answer_refused is not None))
    return judgment


def _unanswered(judgment: Judgment, unreadable: bool) -> str | None:
    """Relay's problem with a required answer, or None when it is usable."""
    role = ROLE_NAMES.get(judgment.by, judgment.by)
    if unreadable:
        return f"the {role}'s answer to the risk hint could not be read"
    if judgment.answer is None:
        return f"the {role} did not answer the risk hint"
    if judgment.answer == "keep" and not judgment.reason:
        return f"the {role} kept the sizing against the risk hint without a reason"
    if judgment.answer == "raise" and not judgment.changed:
        return f"the {role}'s raise names nothing Relay can raise"
    if judgment.answer == "raise" and not judgment.after.risky:
        return f"the {role}'s raise leaves risky at no, so it does not answer the risk hint"
    return None


def owner_change(before: Sizing, size: str | None, risky: bool | None) -> Sizing:
    """The sizing after the owner's `relay resize`: either direction. The
    deploy flag is kept; the note on why the full chain ran would describe
    a sizing that has changed, so it goes."""
    return Sizing(size if size is not None else before.size,
                  risky if risky is not None else before.risky, None, before.deploy)


# ---------------------------------------------------------------------------
# The files backend: raises read back from the reports
# ---------------------------------------------------------------------------

def _report_frontmatter(path) -> dict | None:
    """A report's frontmatter from its first REPORT_HEAD_BYTES, or None
    when it is not a regular file or cannot be read."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "rb") as report:
            head = report.read(REPORT_HEAD_BYTES)
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        return bounded_yaml.frontmatter(text.replace("\r\n", "\n"))
    except (OSError, UnicodeDecodeError, bounded_yaml.NotRead):
        return None


def _judging_reports(task_id: str) -> list[tuple[int, str, object]]:
    """(step, role, path) of the task's Reviewer and Arbiter reports, in step order."""
    task_dir = settings.TASKS_DIR / task_id
    try:
        names = sorted(entry.name for entry in task_dir.iterdir())
    except OSError:
        return []
    found = []
    for name in names:
        match = _JUDGING_REPORT.fullmatch(name)
        if match:
            found.append((int(match.group(1)), match.group(2), task_dir / name))
    return found


def _folded_judgments(brief: Sizing, task_id: str, before_step: int | None = None) -> list[Judgment]:
    """Each report's judgment that changed the sizing, applied in step order."""
    current, changes = brief, []
    for step, role, path in _judging_reports(task_id):
        if before_step is not None and step >= before_step:
            break
        frontmatter = _report_frontmatter(path)
        if frontmatter is None:
            continue
        judgment = judgment_in(frontmatter, current, by=role, step=step, answer_required=False)
        if judgment.changed:
            changes.append(judgment)
            current = judgment.after
    return changes


def _folded(brief: Sizing, task_id: str, before_step: int | None = None) -> Sizing:
    """The brief's sizing with every raise from the reports applied."""
    changes = _folded_judgments(brief, task_id, before_step)
    return changes[-1].after if changes else brief


# ---------------------------------------------------------------------------
# The history of a task's sizing
# ---------------------------------------------------------------------------

def _history_entry(sizing: Sizing, *, before=None, reason=None, by="start", step=None,
                   timestamp=None) -> dict:
    return {"size": sizing.size, "risky": sizing.risky, "deploy": sizing.deploy,
            "from": before, "reason": reason, "trigger": {"by": by, "step": step},
            "timestamp": timestamp}


def _event_history_entry(event: dict) -> dict:
    """One `sized` event as a history entry. Raises ValueError when unreadable."""
    recorded = sizing_of_event(event)
    payload = _payload(event)
    trigger = payload.get("trigger")
    if trigger is None:
        return _history_entry(recorded, timestamp=event.get("timestamp"))
    if not isinstance(trigger, dict) or not isinstance(payload.get("from"), dict):
        raise ValueError("the recorded change could not be read")
    before = payload["from"]
    return _history_entry(recorded, before={"size": before.get("size"), "risky": before.get("risky")},
                          reason=payload.get("reason"), by=trigger.get("by"),
                          step=trigger.get("step"), timestamp=event.get("timestamp"))


def read_sizing_history(task_id: str, config: dict | None) -> list[dict]:
    """The task's sizing over time, oldest first: the start, then every
    change, each {size, risky, deploy, from, reason, trigger: {by, step},
    timestamp}. The start has `from` None and trigger {by: "start"}. An
    event Relay cannot read is one entry {"unreadable": True}, never a
    guess. On the files backend: the brief's sizing, then each raise its
    reports gave (with no timestamp)."""
    if config is not None and settings._sqlite_enabled(config):
        events = sized_events(task_id)
        if not events:
            return [_history_entry(read_brief_sizing(task_id))]
        history = []
        for event in events:
            try:
                history.append(_event_history_entry(event))
            except ValueError:
                history.append({"unreadable": True})
        return history
    brief = read_brief_sizing(task_id)
    return [_history_entry(brief)] + [
        _history_entry(judgment.after, before={"size": judgment.before.size, "risky": judgment.before.risky},
                       reason=judgment.reason, by=judgment.by, step=judgment.step)
        for judgment in _folded_judgments(brief, task_id)]


def first_phase(sizing: Sizing) -> str:
    """Where a task of this size starts."""
    return "builder" if sizing.size == "fix" else settings.FIRST_PHASE


def summary(sizing: Sizing) -> str:
    """One line for `relay start` and `relay status`."""
    line = f"Size: {sizing.size}; risky: {'yes' if sizing.risky else 'no'}"
    return f"{line} ({sizing.note})" if sizing.note else line


def yes_no(risky: bool) -> str:
    return "yes" if risky else "no"


def change_words(before: dict, after: dict) -> str:
    """"size feature -> project; risky no -> yes": each field that changed."""
    parts = []
    if before.get("size") != after.get("size"):
        parts.append(f"size {before.get('size')} -> {after.get('size')}")
    if before.get("risky") != after.get("risky"):
        parts.append(f"risky {yes_no(before.get('risky'))} -> {yes_no(after.get('risky'))}")
    return "; ".join(parts) or "no change"
