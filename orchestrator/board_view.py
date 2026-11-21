"""The words of a task's gate, which `relay waiting`, `relay card` and
`relay decide` (gate_card.py) and `relay status` print: a task's one-line
state, the gate's headline, what the owner is asked, and the findings of
the gate's report.

Every fact comes from the read API (`read_api.list_payload`,
`read_api.status_payload`) or from a report file in the task folder. This
module adds only words and order, so a card can never answer differently
from `relay status --json`.

Reports, notes and task ids are untrusted text. They leave here as plain
strings, and file names only, never a path.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

import agents
import report_reader
import risk_hint
import risk_record
import settings
from arbiter_record import DECISION_WORDS
from printable import QUOTED_LIMIT, printable
from read_api import GATE_READY, GATE_TIME_LIMIT, GATE_WAITING_ON_AGENT, GATE_WAITING_ON_OWNER
from routing import (
    GATE_APPROVAL, GATE_ARBITER_ESCALATION, GATE_ARBITER_NEEDS_OWNER, GATE_ARBITER_UNREADABLE,
    GATE_BLOCKED, GATE_DEPLOY_APPROVAL, GATE_DEPLOY_FLAG_MISSING,
    GATE_FIX_ROUNDS, GATE_OWNER_DECISION, GATE_RETRY_LIMIT, GATE_RISK_UNANSWERED,
)
from run_state import _is_agent_artifact

# A task id reaches the file system as a folder name, so only these
# characters are ever looked up. `relay card` and `relay show` check an id against it.
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# A report's recorded file name: two digits, then a plain name ending .md.
_REPORT_NAME = re.compile(r"[0-9]{2}[A-Za-z0-9._-]*\.md")
MAX_REPORT_BYTES = report_reader.MAX_REPORT_BYTES

# ---------------------------------------------------------------------------
# The words.
# ---------------------------------------------------------------------------

NO_REPORTS = "No reports yet."
PLACE_NOT_GIVEN = "place not given"

# The second line of a "Needs you" row, by gate kind. An approval gate is
# named after the role that stopped, so it is not in this table. {rounds} is
# the automatic rounds that ran before the limit stopped the task, in words;
# {fix_rounds} the fix rounds that ran before a standing fix_rounds gate.
_ROW_LINE_BY_KIND = {
    GATE_DEPLOY_APPROVAL: "Ready to deploy",
    GATE_DEPLOY_FLAG_MISSING: "Plan does not say if it deploys",
    GATE_OWNER_DECISION: "Pick the next phase",
    GATE_RETRY_LIMIT: "Stopped after {rounds}",
    GATE_FIX_ROUNDS: "Still finding highs after {fix_rounds} fix rounds",
    GATE_BLOCKED: "Paused",
    GATE_ARBITER_ESCALATION: "The Arbiter asks you",
    GATE_ARBITER_NEEDS_OWNER: "The Arbiter's ruling needs your yes",
    GATE_ARBITER_UNREADABLE: "Relay could not act on the Arbiter",
    GATE_RISK_UNANSWERED: "Reviewer did not answer the risk hint",
}

# What the owner is being asked, by gate kind. {next} is the next phase
# and {times} how many automatic rounds ran into it, in words.
_ASK_BY_KIND = {
    GATE_APPROVAL: "Approve to go on to {next}, or send it back for fixes.",
    GATE_DEPLOY_APPROVAL: "Ops says this is ready. Deploy it, run the smoke test, then close the task.",
    GATE_DEPLOY_FLAG_MISSING: "The plan does not say whether this task deploys. Add 'deploy: yes' "
                              "or 'deploy: no' to the plan, then run relay next.",
    GATE_OWNER_DECISION: "Relay has no next phase to suggest. Pick one, or abort the task.",
    GATE_RETRY_LIMIT: "{next} went back for fixes {times} in a row without you deciding. "
                      "Send it back for one more round, move it on, or abort.",
    GATE_FIX_ROUNDS: "Send it back for another round, move it somewhere else, or abort.",
    GATE_BLOCKED: "This task is paused. It moves again only when you override it or abort it.",
    GATE_TIME_LIMIT: "Start {next} again, move the task to a phase, or abort.",
    GATE_ARBITER_ESCALATION: "The Arbiter asks you to decide. Its question and choices are below.",
    GATE_ARBITER_NEEDS_OWNER: "{because}. Accept its ruling, send it back, or move it.",
    GATE_ARBITER_UNREADABLE: "Pick the next phase, send it back to the Builder, run the Arbiter "
                             "again, or abort.",
    GATE_RISK_UNANSWERED: "The Reviewer did not answer the risk hint. Approve to go on to {next}, "
                          "send it back, or re-size it with relay resize.",
}
# Ahead of the ask at a retry-limit stop on a task the Arbiter has ruled on.
LAST_RULED = "The Arbiter last ruled (step {step}): {decision}."
# The Arbiter's own words on its gate, each labelled as its words.
ARBITER_REASON_LABEL = "The Arbiter's reason: "
ARBITER_QUESTION_LABEL = "The Arbiter's question: "
ARBITER_FOLLOW_UPS = "Follow-ups the Arbiter recorded"
ARBITER_KNOWN_FLAWS = "Known flaws it accepted"
ARBITER_NONE_RECORDED = "The Arbiter recorded no follow-ups."
# At an escalation, what Relay itself warns of, ahead of the Arbiter's words.
RELAY_WARNS = "Relay warns: {because}."
_ASK_APPROVAL_CLOSES = "Accept these and close the task, or send it back for fixes."

CAPPED_LABEL = "Capped verdict"
CAPPED_SENTENCE = ("Security found nothing to fix. The verdict reads 'findings' only because this "
                   "project's profile names no safe test target, so no live attack was run.")
# A report that says it is capped and still lists findings: the board says
# what is shown, not that nothing was found.
CAPPED_BUT_LISTS = "Security marked this verdict as capped but lists {findings}. Read them."
CAPPED_NOT_CONFIRMED = ("Security marked this verdict as capped. The board cannot confirm from "
                        "the report that it found nothing to fix. Read the report.")

SEVERITY_TITLES = {"high": "High", "medium": "Medium", "low": "Low"}
NOT_RATED_TITLE = "Not rated"
WRITTEN_BEFORE_RATING = "This report was written before findings were rated."
FINDINGS_ARE_BELOW = WRITTEN_BEFORE_RATING + " Its findings are in the report below."
NO_FINDINGS = "No findings at this gate. The report is below."
# The board says a report has no findings, or that its list is the whole
# list, only on things the report's text cannot fake: see _may_say_none and
# report_reader.read_report. In every other case it says one of these.
VERDICT_BUT_NO_ROWS = ("{role}'s verdict is '{verdict}', but the board read no rated findings in "
                       "its report. Read the report below.")
NONE_NOT_CONFIRMED = ("The board could not read this report's findings with confidence, so it "
                      "cannot say there are none. Read the report below.")
ROWS_MAY_NOT_BE_ALL = ("The board may not have read every finding in this report. "
                       "Read the report below.")
SUMMARY_NOT_READ = ("{fallback} The report's summary line could not be read with confidence, "
                    "so it is not shown.")
# Roles whose reports list findings, with the verdict each gives when it
# found nothing, or nothing rated above low. Under any other verdict the
# board never says nothing was found, unless Security declared a cap.
_NOTHING_FOUND_VERDICT = {"reviewer": "approved", "tester": "pass", "security": "secure",
                          "pentest": "secure"}
_ROLES_WITH_FINDINGS = tuple(_NOTHING_FOUND_VERDICT)

FINDINGS_SECTION = "Findings"


def role_title(phase) -> str:
    return str(phase).capitalize() if phase else "Unknown role"


def verdict_in_words(verdict) -> str | None:
    """"approved_with_warnings" as "Approved with warnings"."""
    if not verdict:
        return None
    words = str(verdict).replace("-", " ").replace("_", " ")
    return words[:1].upper() + words[1:]


def _verdict_mid_sentence(verdict) -> str:
    """The same words without the capital, for use after a colon."""
    return (verdict_in_words(verdict) or "no verdict").lower()


# ---------------------------------------------------------------------------
# Times: the Mac's local time, short for today and a date for other days
# ---------------------------------------------------------------------------
# `now` is the clock the caller reads. `relay serve` passes the plain local
# time (no zone), and each timestamp is then converted by the system's own
# rules for its date, daylight saving included. Tests pass a `now` with a
# fixed zone so their answers do not depend on the machine.

_DATE_ONLY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _parsed(timestamp, now: datetime) -> datetime | None:
    """A ledger timestamp in local time, or None when it cannot be read as
    one. A time without a zone is taken as UTC, which is what Relay writes;
    a bare date (old run.yaml files hold some) is taken as that local day."""
    if timestamp is None:
        return None
    text = str(timestamp).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if _DATE_ONLY.fullmatch(text):
            return parsed.replace(tzinfo=now.tzinfo) if now.tzinfo else parsed.astimezone()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(now.tzinfo)
    except (OverflowError, OSError, ValueError):
        return None


def _day(local: datetime, now: datetime) -> str:
    """"Oct 4"; the year is added only when it is not this year."""
    day = f"{local:%b} {local.day}"
    return day if local.year == now.year else f"{day}, {local.year}"


def _when(timestamp, now: datetime) -> dict | None:
    """A time for display: `short` ("06:06" today, "Oct 4" otherwise) and
    `full`. A value that is not a time is shown as written."""
    if timestamp is None or not str(timestamp).strip():
        return None
    local = _parsed(timestamp, now)
    if local is None:
        return {"short": str(timestamp), "full": str(timestamp)}
    short = f"{local:%H:%M}" if local.date() == now.date() else _day(local, now)
    return {"short": short, "full": f"{local:%Y-%m-%d %H:%M}"}


def _clock_or_date(timestamp, now: datetime) -> str | None:
    """For a sentence: the time when it was today, the date and time otherwise."""
    when = _when(timestamp, now)
    if when is None:
        return None
    local = _parsed(timestamp, now)
    return when["short"] if local is not None and local.date() == now.date() else when["full"]


def _newest_first(rows: list[dict], now: datetime) -> list[dict]:
    """Most recently updated first; a task with no readable date is last.
    Ties and the undated go by task id."""
    def key(row):
        updated = _parsed(row["updated"], now)
        return (updated is None, -updated.timestamp() if updated else 0, row["task_id"])
    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# A task's one-line state
# ---------------------------------------------------------------------------

def row_line(row: dict, now: datetime) -> str:
    """The second line of a task's row: why it is where it is."""
    latest = row["latest_step"] or {}
    role = role_title(latest.get("phase") or row["current_phase"])
    if row["gate_state"] == GATE_WAITING_ON_OWNER:
        if row["gate_kind"] == GATE_TIME_LIMIT:
            stop = row.get("time_limit") or {}
            return (f"Stopped: {role_title(stop.get('phase'))} ran past its "
                    f"{agents.limit_in_words(stop.get('limit_seconds'))} limit")
        if row["gate_kind"] in _ROW_LINE_BY_KIND:
            rounds = row["automatic_rounds"]
            return _ROW_LINE_BY_KIND[row["gate_kind"]].format(
                rounds="1 automatic round" if rounds == 1 else f"{rounds} automatic rounds",
                fix_rounds=row.get("fix_rounds"))
        return f"{role} gate"
    if row["gate_state"] == GATE_WAITING_ON_AGENT:
        since = _when(latest.get("started"), now)
        return f"{role}, since {since['short']}" if since else f"{role}, start time not recorded"
    if row["gate_state"] == GATE_READY:
        return f"{role} finished: {_verdict_mid_sentence(latest.get('verdict'))}"
    closed = "Abandoned" if row["status"] == "abandoned" else "Done"
    updated = _when(row["updated"], now)
    return f"{closed} {updated['short']}" if updated else f"{closed}, date not recorded"


# ---------------------------------------------------------------------------
# Reading a report file safely
# ---------------------------------------------------------------------------

def _read_report_file(task_dir: Path, name) -> tuple[str | None, bool]:
    """A report's text, as (text, too_large). The name comes from the
    ledger, which is still not trusted with the file system: it must be a
    bare artifact file name, and the file a regular one (no link) sitting
    directly in the task folder. Anything else reads as no report.

    Bytes that are not UTF-8 are shown as replacement marks, not refused."""
    if not isinstance(name, str) or not _REPORT_NAME.fullmatch(name):
        return None, False
    try:
        if not _is_agent_artifact(task_dir / name):
            return None, False
    except (OSError, ValueError):
        return None, False
    return read_task_file(task_dir, name)


def read_task_file(task_dir: Path, name: str) -> tuple[str | None, bool]:
    """A file directly in the task folder, by a name already checked, as
    (text, too_large): a regular file only, never a link, up to 1 MB."""
    candidate = task_dir / name
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None, False
        if candidate.resolve(strict=True).parent != task_dir.resolve(strict=True):
            return None, False
        with open(candidate, "rb") as report_file:
            content = report_file.read(MAX_REPORT_BYTES + 1)
    except (OSError, ValueError):
        return None, False
    if len(content) > MAX_REPORT_BYTES:
        return None, True
    return content.decode("utf-8", errors="replace"), False


def _too_large(name: str) -> str:
    return f"{name} is larger than 1 MB, so it is not shown here."


class _Reports:
    """The task's report files, each read and parsed at most once."""

    def __init__(self, task_id: str):
        self._task_dir = settings.TASKS_DIR / task_id
        self._read = {}

    def get(self, name) -> dict | None:
        """{"report": parsed or None, "too_large": bool} for a file that is
        there to show, or None when there is no report under that name."""
        if name not in self._read:
            text, too_large = _read_report_file(self._task_dir, name)
            if text is None and not too_large:
                self._read[name] = None
            else:
                self._read[name] = {"too_large": too_large,
                                    "report": None if too_large else report_reader.read_report(text)}
        return self._read[name]


# ---------------------------------------------------------------------------
# What the owner is asked
# ---------------------------------------------------------------------------

def _next_phase_title(next_phase) -> str:
    return role_title(next_phase) if next_phase else "the next phase"


def _ask(gate: dict, retries: dict, rulings: list | None = None) -> str:
    """What the owner is asked. At a standing fix_rounds gate, how many fix
    rounds have run comes first; at a retry-limit stop, the Arbiter's latest
    ruling, if it has ruled."""
    return _fix_rounds_listed(gate) + _last_ruled(gate, rulings or []) + _ask_for_kind(gate, retries)


def _last_ruled(gate: dict, rulings: list) -> str:
    if gate["kind"] != GATE_RETRY_LIMIT:
        return ""
    ruled = [ruling for ruling in rulings if ruling.get("decision") in DECISION_WORDS]
    if not ruled:
        return ""
    return LAST_RULED.format(step=ruled[-1]["step"], decision=DECISION_WORDS[ruled[-1]["decision"]]) + " "


def _ask_for_kind(gate: dict, retries: dict) -> str:
    if gate["kind"] == GATE_APPROVAL and gate["next_phase"] == "done":
        return _ASK_APPROVAL_CLOSES
    sentence = _ASK_BY_KIND.get(gate["kind"], "Relay stopped here for you to decide how to go on.")
    rounds = retries.get(gate["next_phase"], 0)
    because = "; ".join((gate.get("arbiter") or {}).get("needs_owner_because") or [])
    return sentence.format(next=_next_phase_title(gate["next_phase"]),
                           times="1 time" if rounds == 1 else f"{rounds} times",
                           because=because[:1].upper() + because[1:])


def _fix_rounds_listed(gate: dict) -> str:
    """The tripwire's sentences, ahead of the ask."""
    listed = gate.get("fix_rounds")
    if not listed:
        return ""
    return " ".join(listed["lines"]) + " "


def _gate_headline(latest: dict | None, report: dict | None) -> str:
    """The role's own summary line when its report has one that can be
    told from quoted text; otherwise the board's own sentence."""
    if report and report["summary_line"]:
        return report["summary_line"]
    if latest is None:
        return "This task is waiting on you."
    role = role_title(latest["phase"])
    fallback = (f"{role} stopped before giving a verdict." if latest["verdict"] is None
                else f"{role} stopped: {_verdict_mid_sentence(latest['verdict'])}.")
    if report and report["summary_in_doubt"]:
        return SUMMARY_NOT_READ.format(fallback=fallback)
    return fallback


def _not_waiting_sentence(status: dict, now: datetime) -> str:
    """One plain sentence for a task that is not waiting on the owner."""
    task, gate, latest = status["task"], status["gate"], status["latest_step"] or {}
    role = role_title(latest.get("phase") or task["current_phase"])
    if gate["state"] == GATE_WAITING_ON_AGENT:
        started = _clock_or_date(latest.get("started"), now)
        opening = (f"{role} started at {started}." if started
                   else f"{role} is running. Its start time was not recorded.")
        return f"{opening} No report yet. Nothing for you to do."
    if gate["state"] == GATE_READY:
        moves = ("Relay closes the task" if gate["next_phase"] == "done"
                 else f"Relay moves on to {_next_phase_title(gate['next_phase'])}")
        return (f"{role} finished: {_verdict_mid_sentence(latest.get('verdict'))}. "
                f"{moves} the next time it is run. Nothing for you to decide.")
    closed = "Abandoned" if task["status"] == "abandoned" else "Done"
    updated = _parsed(task["updated"], now)
    if updated is None:
        return f"{closed}. The date was not recorded."
    return f"{closed} on {updated:%b} {updated.day}, {updated.year}."


def _is_capped(step: dict, report: dict | None) -> bool:
    """Security said its verdict is `findings` only for want of a safe test target."""
    return (step["phase"] == "security" and step["verdict"] == "findings"
            and report is not None and report["capped_declared"] is True)


def _may_say_none(phase, verdict, report: dict) -> bool:
    """Whether the board may say this rated report has no findings. The
    report must say so in the one section the reader took, with nothing
    else in the file that could be a second section or a row. And the
    verdict, which comes from the ledger and not from the report's body,
    must agree: a role that lists findings gave the verdict it gives when
    it found nothing, or Security declared its verdict capped."""
    if not (report["says_none"] and report["findings_complete"]):
        return False
    if phase not in _ROLES_WITH_FINDINGS:
        return True
    return verdict == _NOTHING_FOUND_VERDICT[phase] or _is_capped({"phase": phase, "verdict": verdict}, report)


def _no_rows_message(phase, verdict, report: dict) -> str:
    """What the gate says for a rated report in which no row was read."""
    if _may_say_none(phase, verdict, report):
        return NO_FINDINGS
    if phase in _ROLES_WITH_FINDINGS and verdict and verdict != _NOTHING_FOUND_VERDICT[phase]:
        return VERDICT_BUT_NO_ROWS.format(role=role_title(phase), verdict=verdict)
    return NONE_NOT_CONFIRMED


def _section_blocks(report: dict, section: dict) -> list[dict]:
    """A section's blocks, without its heading."""
    return report["blocks"][section["first"] + 1:section["last"] + 1]


def _gate_findings(latest: dict | None, read: dict | None) -> dict:
    """Findings for the gate view: rated ones by severity, highest first;
    everything else under "Not rated", as written. Old prose is never
    split into rows or counted. `read` is what _Reports.get gave for the
    gate's report."""
    if read is None:
        return {"groups": [], "not_rated": None, "empty_message": NO_REPORTS}
    if read["too_large"]:
        return {"groups": [], "not_rated": None, "empty_message": _too_large(latest["artifact"])}
    phase, report = latest["phase"], read["report"]
    groups = []
    for severity in report_reader.SEVERITIES:
        items = [{"number": finding["number"], "statement": finding["statement"],
                  "place": finding["place"] or PLACE_NOT_GIVEN}
                 for finding in report["findings"] if finding["severity"] == severity]
        if items:
            groups.append({"severity": severity, "title": SEVERITY_TITLES[severity],
                           "count": len(items), "items": items})

    not_rated = None
    if report["unreadable_rows"]:
        not_rated = {"title": NOT_RATED_TITLE, "count": len(report["unreadable_rows"]),
                     "note": None, "rows": report["unreadable_rows"], "blocks": []}
    old_findings = None if report["rated"] else report_reader.find_sections(
        report, [FINDINGS_SECTION])[FINDINGS_SECTION]
    if old_findings:
        not_rated = {"title": NOT_RATED_TITLE, "count": None, "note": WRITTEN_BEFORE_RATING,
                     "rows": [], "blocks": _section_blocks(report, old_findings)}

    # The sentence under the list: why it is empty, or that it may not be whole.
    empty_message = None
    if not groups and not_rated is None:
        if report["rated"]:
            empty_message = _no_rows_message(phase, latest["verdict"], report)
        else:
            empty_message = FINDINGS_ARE_BELOW if phase in _ROLES_WITH_FINDINGS else NO_FINDINGS
    elif report["rated"] and not report["findings_complete"]:
        empty_message = ROWS_MAY_NOT_BE_ALL
    return {"groups": groups, "not_rated": not_rated, "empty_message": empty_message}


def _capped_sentence(findings: dict) -> str:
    """What the board says beside the capped label. It says Security found
    nothing only when the board itself lists nothing from the report."""
    if findings["empty_message"] == NO_FINDINGS:
        return CAPPED_SENTENCE
    not_rated = findings["not_rated"] or {}
    listed = sum(group["count"] for group in findings["groups"]) + (not_rated.get("count") or 0)
    if not listed:
        return CAPPED_NOT_CONFIRMED
    return CAPPED_BUT_LISTS.format(findings="1 finding" if listed == 1 else f"{listed} findings")


def _duration_words(seconds) -> str:
    """"121 minutes", "1 minute" or "45 seconds"."""
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        return "a time Relay could not read"
    if seconds < 60:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    minutes = seconds // 60
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _time_limit_view(status: dict) -> dict:
    """The view of a task a role's time limit stopped. The report the role
    left is never read: the card names it and says it is not used."""
    gate = status["gate"]
    stop = gate["time_limit"]
    if stop["report_left"]:
        left = (f"A report was left; it is not used. Read it with: {settings._CLI_CMD} show "
                f"{status['task']['task_id']} {stop['report']}")
    else:
        left = "No report was left."
    return {"waiting": True,
            "headline": f"The {role_title(stop['phase'])} ran past its time limit",
            "ask": _ask(gate, status["retries"]),
            "capped": None,
            "relay_warning": None,
            "arbiter_lines": [],
            "findings": {"groups": [], "not_rated": None,
                         "empty_message": (f"Limit: {_duration_words(stop['limit_seconds'])}. "
                                           f"It ran for {_duration_words(stop['ran_seconds'])}. "
                                           f"{left}")}}


def _arbiter_findings(ruling: dict) -> dict:
    """The findings of a gate the Arbiter's step stopped at: the follow-ups
    and known flaws it recorded, each its own words."""
    groups = []
    for title, field in ((ARBITER_FOLLOW_UPS, "follow_ups"), (ARBITER_KNOWN_FLAWS, "known_flaws")):
        items = [{"number": number, "statement": printable(text, QUOTED_LIMIT), "place": PLACE_NOT_GIVEN}
                 for number, text in enumerate(ruling.get(field) or [], 1)]
        if items:
            groups.append({"severity": None, "title": title, "count": len(items), "items": items})
    return {"groups": groups, "not_rated": None,
            "empty_message": None if groups else ARBITER_NONE_RECORDED}


def _relay_warning(gate: dict) -> str | None:
    """At an escalation, Relay's own warnings (needs_owner_because) in one
    line, so the owner never has only the Arbiter's framing to go on.
    Other Arbiter gates already put them in the ask."""
    because = (gate.get("arbiter") or {}).get("needs_owner_because")
    if gate["kind"] != GATE_ARBITER_ESCALATION or not because:
        return None
    return RELAY_WARNS.format(because="; ".join(because))


def _arbiter_lines(ruling: dict | None) -> list[str]:
    """The Arbiter's reason, and its question at an escalation, labelled."""
    if not ruling:
        return []
    lines = []
    if ruling.get("reason"):
        lines.append(ARBITER_REASON_LABEL + printable(ruling["reason"], QUOTED_LIMIT))
    if ruling.get("question"):
        lines.append(ARBITER_QUESTION_LABEL + printable(ruling["question"], QUOTED_LIMIT))
    return lines


def _view(status: dict, gate_read: dict | None, now: datetime) -> dict:
    gate, latest = status["gate"], status["latest_step"]
    gate_report = gate_read["report"] if gate_read else None
    if gate["state"] != GATE_WAITING_ON_OWNER:
        return {"waiting": False, "headline": _not_waiting_sentence(status, now),
                "ask": None, "capped": None, "findings": None, "relay_warning": None,
                "arbiter_lines": []}
    if gate["kind"] == GATE_TIME_LIMIT:
        return _time_limit_view(status)
    ruling = gate.get("arbiter")
    capped = latest is not None and _is_capped(latest, gate_report)
    findings = _arbiter_findings(ruling) if ruling else _gate_findings(latest, gate_read)
    return {"waiting": True,
            "headline": _gate_headline(latest, gate_report),
            "ask": _ask(gate, status["retries"], status.get("arbiter_rulings")),
            "capped": ({"label": CAPPED_LABEL, "sentence": _capped_sentence(findings)}
                       if capped else None),
            "relay_warning": _relay_warning(gate),
            "arbiter_lines": _arbiter_lines(ruling),
            "sizing_lines": _sizing_lines(status),
            "findings": findings}


# The most paths of one category the card shows at the unanswered gate.
CARD_PATHS_PER_CATEGORY = 3


def _since_the_last_approval(status: dict, timestamp) -> bool:
    approvals = [approval["timestamp"] for approval in status.get("approvals") or []
                 if approval.get("timestamp")]
    return not approvals or (timestamp is not None and str(timestamp) > max(approvals))


def _sizing_lines(status: dict) -> list[str]:
    """What this gate shows of the sizing: each change, keep and refused
    lowering since the owner's last approval and, where the Reviewer did not
    answer the hint, the hint with a few of the branch's paths. Every
    role's word and path leaves through printable (risk_record)."""
    lines = [f"Sizing changed: {risk_record.change_line(entry)}"
             for entry in (status.get("sizing_history") or [])[1:]
             if _since_the_last_approval(status, entry.get("timestamp"))]
    for judged in status.get("sizing_judgments") or []:
        if _since_the_last_approval(status, judged.get("timestamp")):
            lines += risk_record.judgment_lines(judged, with_unanswered=False)
    if status["gate"]["kind"] == GATE_RISK_UNANSWERED:
        lines.append(risk_hint.summary(status.get("risk_hint")))
        lines += risk_record.hint_lines(status.get("risk_hint"), CARD_PATHS_PER_CATEGORY)
    return lines


# ---------------------------------------------------------------------------
# The gate view
# ---------------------------------------------------------------------------

def gate_view(status: dict, now: datetime, reports: _Reports | None = None) -> dict:
    """The gate view of a task's status, which the gate card (gate_card.py)
    is made from."""
    latest = status["latest_step"]
    reports = reports or _Reports(status["task"]["task_id"])
    stopped = status["gate"]["kind"] == GATE_TIME_LIMIT
    gate_read = reports.get(latest["artifact"]) if latest and not stopped else None
    return _view(status, gate_read, now)
